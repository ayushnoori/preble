from collections import defaultdict
from datetime import datetime, timedelta
from greedy_lp import RequestFuncOutput
from global_lru_cache import LPRadixCache, LPTreeNode
import time
import numpy as np
import threading
import heapq
from collections import deque
from typing import List, Tuple
from transformers import AutoTokenizer
tokenizer = AutoTokenizer.from_pretrained("mistralai/Mistral-7B-v0.1")

class SlidingWindowHistogram:
    def __init__(self, window_duration: timedelta, gpu_allocations, num_gpus=2):
        self.window_duration = window_duration
        self.gpu_allocations = gpu_allocations
        self.histogram = defaultdict(int)
        self.node_to_count = defaultdict(int)

        self.timestamps: List[Tuple[datetime, LPTreeNode, LPTreeNode]] = []
        self.num_gpus = num_gpus

    def update(self, timestamp, node: LPTreeNode, leaf_node: LPTreeNode):
        self.timestamps.append((timestamp, node, leaf_node))
        self.histogram[node] += 1 * leaf_node.context_length
        self.node_to_count[node] += 1

        self._remove_old_entries(timestamp)

    def _remove_old_entries(self, current_timestamp):
        window_start = current_timestamp - self.window_duration
        while self.timestamps and self.timestamps[0][0] < window_start:
            timestamp, node, leaf_node = self.timestamps.pop(0)
            self.histogram[node] -= 1 * leaf_node.context_length
            self.node_to_count[node] -= 1
            if self.histogram[node] <= 0:
                del self.histogram[node]
                del self.node_to_count[node]
                self.gpu_allocations[node] = set() # Reset the gpu allocation outside the time window

    def rename_node(self, old_node, new_node):
        if old_node in self.histogram:
            self.histogram[new_node] = self.histogram.pop(old_node)
            self.node_to_count[new_node] = self.node_to_count.pop(old_node)
            timestamps = []
            for timestamp, important_node, leaf_node in self.timestamps:
                if important_node == old_node:
                    important_node = new_node
                timestamps.append((timestamp, important_node, leaf_node))
            self.timestamps = timestamps

    def query(self):
        return dict(self.histogram)

    def current_allocation_per_gpu(self):
        allocation = [0 for _ in range(self.num_gpus)]
        node: LPTreeNode
        for node, cost in self.histogram.items():
            for gpu in self.gpu_allocations.get(node, {}):
                allocation[gpu] += cost / len(self.gpu_allocations.get(node))# potentionally divide by length of node.cached_gpus here
        return allocation

    def current_allocation_per_gpu_with_atleast_min_load(self, min_load=2):
        allocation = [0 for _ in range(self.num_gpus)]
        node: LPTreeNode
        for node, cost in self.histogram.items():
            if self.node_to_count[node] < min_load:
                continue
            for gpu in self.gpu_allocations.get(node, {}):
                allocation[gpu] += cost / len(self.gpu_allocations.get(node))# potentionally divide by length of node.cached_gpus here
        return allocation


class TTFTWindowedOverloadedDetector:
    # TTFT is a good indicator of overloaded

    def __init__(self, window_duration=timedelta(minutes=3)):
        self.data = {}
        self.window_duration = window_duration
        self.half_window_duration = window_duration / 2

    def add_data_point(self, timestamp, node, gpu, value):
        """ Add a new data point and remove outdated entries. """
        key = (node, gpu)
        if key not in self.data:
            self.data[key] = deque()
        self.data[key].append((timestamp, value))
        self.purge_old_data(key, timestamp)

    def purge_old_data(self, key, current_time):
        """ Remove data points that are older than the time window. """
        while self.data[key] and self.data[key][0][0] < current_time - self.window_duration:
            self.data[key].popleft()

    def rename_node(self, old_node, new_node, runtime_idx):
        old_key = (old_node, runtime_idx)
        new_key = (new_node, runtime_idx)
        if old_key in self.data:
            self.data[new_key] = self.data.pop(old_key)

    def calculate_half_window_averages(self, key):
        """ Calculate averages for the first and second halves of the window. """
        first_half = []
        second_half = []
        half_window_cutoff = datetime.now() - self.half_window_duration
        if key not in self.data:
            return None, None
        for timestamp, value in self.data[key]:
            if timestamp < half_window_cutoff:
                first_half.append(value)
            else:
                second_half.append(value)
        if len(first_half) == 0:
            return None, None
        if len(second_half) == 0:
            return  None, None
        avg_first_half = sum(first_half) / len(first_half) if first_half else 0
        avg_second_half = sum(second_half) / len(second_half) if second_half else 0

        return avg_first_half, avg_second_half
    
    def delete_after_allocation(self, node, gpu):
        key = (node, gpu)
        if key in self.data:
            del self.data[key]

    def is_node_overloaded(self, node, gpu):
        """ Check if node is overloaded """
        key = (node, gpu)
        avg_first_half, avg_second_half = self.calculate_half_window_averages(key)
        if avg_first_half is None and avg_second_half is None:
            return False
        return avg_second_half >= 2 * avg_first_half


class GlobalScheduler:
    def __init__(self, num_nodes=2, enable_eviction=False, enable_rebalancing=True) -> None:
        self.num_gpus = num_nodes
        self.gpu_allocations = {}
        self.counter = 0
        self.enable_eviction = enable_eviction
        self.per_gpu_load = {i: 0 for i in range(num_nodes)}
        self.all_gpus = set(range(num_nodes))

        self.mem_cost = [0 for _ in range(num_nodes)]
        self.num_gpus = num_nodes
        self.metrics_dict = []
        self.lock = threading.Lock()
        self.histogram = SlidingWindowHistogram(window_duration=timedelta(minutes=3), gpu_allocations=self.gpu_allocations, num_gpus=self.num_gpus)
        self.cache = LPRadixCache(histogram=self.histogram, num_gpus=self.num_gpus)
        self.max_tokens_gpu = [198516 for _ in range(num_nodes)]
        self.HIGH_LOAD_THRESHOLD = 1.4
        self.overload_detector = TTFTWindowedOverloadedDetector(window_duration=timedelta(minutes=3))
        self.enable_rebalancing = enable_rebalancing

    # Consider Split nodes
    def handle_split_nodes_gpu_allocations(self, split_nodes, gpu_allocations):
        for child_node, new_node in split_nodes.items():
            gpu_allocations[new_node] = gpu_allocations[child_node].copy()

    def handle_split_node_histogram(self, split_nodes):
        for child, parent_node in split_nodes.items():
            if self.is_large_node(parent_node) and not self.is_large_node(child): # new node is parent is now larger
                self.histogram.rename_node(child, parent_node)
                for gpu in self.gpu_allocations.get(child, {}):
                    self.overload_detector.rename_node(child, parent_node, gpu)

    # Recursively update get/update parent gpu allocation
    def get_parent_gpu_allocation(self, node: LPTreeNode):
        if not node:
            return self.all_gpus
        if self.gpu_allocations.get(node):
            return self.gpu_allocations.get(node)
        return self.get_parent_gpu_allocation(node.parent)

    def update_gpu_cache_for_parent(self,node: LPTreeNode, gpu_id):
        if not node:
            return
        node.cached_gpus = node.cached_gpus.union(gpu_id)
        self.update_cached_gpus_of_parent(node.parent, gpu_id)

    def update_gpu_allocation_for_parent(self, node: LPTreeNode, gpu_id):
        if not node:
            return
        self.gpu_allocations[node] = self.gpu_allocations.get(node, set()).union(gpu_id)
        self.update_gpu_allocation_for_parent(node.parent, gpu_id)

    def is_small_node(self, node: LPTreeNode):
        return not self.is_large_node(node)

    def is_large_node(self, node: LPTreeNode):
        return node.num_tokens > node.context_length - node.num_tokens

    def get_important_node(self, node: LPTreeNode):
        if self.is_large_node(node):
            return node
        return self.get_important_node(node.parent)

    def get_recomp_cost(self, node: LPTreeNode, gpu_id, histogram: SlidingWindowHistogram):
        if not node:
            return 0
        if node.has_cached_gpu(gpu_id):
            return 0
        else:
            return histogram.histogram.get(node, 1) + self.get_recomp_cost(node.parent, gpu_id, histogram)

    def get_recomp_cost_basic(self, node: LPTreeNode, gpu_id):
        if not node:
            return 0
        if node.has_cached_gpu(gpu_id):
            return 0
        else:
            return node.num_tokens * node.ref_counter[gpu_id] + self.get_recomp_cost_basic(node.parent, gpu_id)

    def evict_callback(self, node: LPTreeNode, runtime_selected: int):
        """Method to handle eviction logic."""
        # TODO: Maybe update the parent if child has no parent
        node.evicted_gpus.add(runtime_selected)
        node.cached_gpus.remove(runtime_selected)
        def remove_allocations_recursive_for_children(node: LPTreeNode, runtime_selected):
            if node not in self.gpu_allocations or runtime_selected not in self.gpu_allocations[node]:
                return
            self.gpu_allocations[node].remove(runtime_selected)
            for child in node.children.values():
                self.remove_allocations_recursive_for_children(child, runtime_selected)
        if self.is_large_node(node):
            remove_allocations_recursive_for_children(node, runtime_selected)
            # Remove all of it's children as well?
        return len(node.value)


    def handle_eviction(self, runtime_selected):
        current_max_tokens = self.max_tokens_gpu[runtime_selected]
        assert self.cache.allocated_size(runtime_selected) >= 0
        if self.cache.allocated_size(runtime_selected) > current_max_tokens:
            num_tokens = self.cache.allocated_size(runtime_selected) - current_max_tokens
            self.cache.evict_with_runtime_id_without_removing(num_tokens, lambda node: self.evict_callback(node, runtime_selected), runtime_selected)
            print(f"GPU {runtime_selected} Evictable size: ", self.cache.allocated_size(runtime_selected), current_max_tokens)

    def runtime_selector(
        self,
        text: str = None,
        request_id: str = None,
        input_ids=None,
        sampling_params=None,
        runtime_id_with_highest_hit_rate=None,
        *args, **kwargs,
    ):
        # Tokenize the text
        start_time = time.time()
        with self.lock:
            split_nodes = {}
            leaf_node = self.cache.insert(tuple(input_ids), split_nodes=split_nodes)
            self.handle_split_nodes_gpu_allocations(split_nodes, self.gpu_allocations) # copies split node gpu allocation
            self.handle_split_node_histogram(split_nodes)

            important_node = self.get_important_node(leaf_node)
            if leaf_node.num_tokens < leaf_node.context_length - leaf_node.num_tokens: # check that gpu allocation exists for important node
                gpu_selected = self.get_parent_gpu_allocation(leaf_node)
            else:
                if runtime_id_with_highest_hit_rate is None:
                    recom_costs = []
                    for gpu_id in range(self.num_gpus):
                        recomputation_cost = self.get_recomp_cost_basic(leaf_node.parent, gpu_id)
                        recom_costs.append(recomputation_cost)
                    histogram_mem_cost = self.histogram.current_allocation_per_gpu()
                    gpu_selected = int(np.argmin([recom_costs[gpu_id] + histogram_mem_cost[gpu_id] for gpu_id in range(self.num_gpus)]))
                    gpu_selected = set([gpu_selected])
                else:
                    gpu_selected = set([runtime_id_with_highest_hit_rate])

            self.histogram.update(datetime.now(), important_node, leaf_node)
            self.update_gpu_allocation_for_parent(leaf_node, gpu_selected)
            runtime_idx = list(gpu_selected)[0]
            if len(gpu_selected) > 1:
                runtime_idx = int(np.random.choice(list(gpu_selected)))

            self.per_gpu_load[runtime_idx] += 1
            self.cache.update_allocated_size(leaf_node, runtime_idx)
    
            if self.enable_rebalancing:
                self.handle_important_node_stealing(runtime_idx)

            self.counter += 1    
            if self.counter % 500:
                # print(self.per_gpu_load)
                pass
            # self.handle_work_stealing(runtime_idx)

        self.metrics_dict.append(
            {
                "text": text,
                "rid": request_id,
                "selected_runtime": runtime_idx,
                "overhead": time.time() - start_time,
            }
        )
        return runtime_idx
    
    def finish_request(
        self, text: str = None, request_id: str = None, input_ids=None, func_output: RequestFuncOutput=None
    ):
        with self.lock:
            runtime_id = func_output.runtime_selected
            self.update_overload_detector(input_ids, runtime_id, func_output)
            self.cache.remove_completed_input_ids(input_ids, runtime_id)

    def handle_important_node_stealing(self, scheduled_idx):
        if sum(self.per_gpu_load.values()) < 50:
            return
        allocation_cost_per_gpu = self.histogram.current_allocation_per_gpu()
        allocations_with_indices = [(gpu_id, allocation_cost_per_gpu[gpu_id]) for gpu_id in range(len(allocation_cost_per_gpu))]
        allocations_with_indices = list(sorted(allocations_with_indices, key=lambda x: -x[1]))
        self.handle_important_node_stealing_recursive(allocations_with_indices)

    def handle_important_node_stealing_recursive(self, allocation_cost_with_devices):
        if len(allocation_cost_with_devices) == 1:
            return
        larger_device, larger_allocation_cost = allocation_cost_with_devices[0]
        smaller_device, smaller_device_allocation_cost = allocation_cost_with_devices[-1] # Last element is the smallest

        if larger_allocation_cost < self.HIGH_LOAD_THRESHOLD * smaller_device_allocation_cost:
            return
        # if self.per_gpu_load[larger_device] < self.HIGH_LOAD_THRESHOLD * self.per_gpu_load[smaller_device]:
        #     return
        
        # Use a min heap to manage node costs
        node_cost_for_gpu = []
        for node, cost in self.histogram.histogram.items():
            # If after adjusting the nodes, the allocation difference is valid, allow adjustment
            if larger_device in self.gpu_allocations.get(node) and self.histogram.node_to_count[node] > 1:
                heapq.heappush(node_cost_for_gpu, (cost, node))

        if len(node_cost_for_gpu) == 1:
            # Handle load splitting a single node in two
            cost, node = node_cost_for_gpu[0] 
            cost /= 2 # load is now split into two
            # if not node.has_cached_gpu(smaller_device) and self.overload_detector.is_node_overloaded(node, larger_device): 
            if smaller_device not in self.gpu_allocations[node] and self.overload_detector.is_node_overloaded(node, larger_device): 
                # Copying the node to the smallest device will not change the larger allocation
                larger_allocation_cost -= cost
                smaller_device_allocation_cost += cost
                self.gpu_allocations[node].add(smaller_device)
                self.overload_detector.delete_after_allocation(node, larger_device)
        else:
            while node_cost_for_gpu:
                node: LPTreeNode
                cost, node = heapq.heappop(node_cost_for_gpu)

                assert self.is_large_node(node)
                # if node.has_cached_gpu(smaller_device): # Avoid copying an existing device
                if smaller_device in self.gpu_allocations[node]:
                    continue

                if larger_allocation_cost - cost < smaller_device_allocation_cost + cost:
                    break
                larger_allocation_cost -= cost
                smaller_device_allocation_cost += cost
                self.gpu_allocations[node] = {smaller_device}
                self.update_children(node, smaller_device)
        
                # if larger_allocation_cost < self.HIGH_LOAD_THRESHOLD * smaller_device_allocation_cost:
                #     return
            # Upstead the sorted allocation based on the new smallest allocation
        allocation_cost_with_devices[0] = (larger_device, larger_allocation_cost)
        allocation_cost_with_devices[-1] = (smaller_device, smaller_device_allocation_cost)
        self.handle_important_node_stealing_recursive(allocation_cost_with_devices[1:])

    def update_children(self, node: LPTreeNode, gpu_id):
        for child in node.children.values():
            self.gpu_allocations[child] = {gpu_id}
            self.update_children(child, gpu_id)
    
    def print(self):
        self._print_helper(self.cache.root_node, 0)

    def _print_helper(self, node: LPTreeNode, indent=0, depth=0):
        for key, child in node.children.items():
            allocated_gpus = self.gpu_allocations.get(child, set())
            print(f"{' ' * indent}{tokenizer.decode(child.value)[:20].strip()} Cached: {child.cached_gpus} Allocated: {allocated_gpus} Evicted: {child.evicted_gpus} {len(child.value)}")
            self._print_helper(child, indent=indent + 2, depth=depth + 1)

    def work_steal_low_loaded_prefixes(self):
        low_load_nodes = []
        max_req = max(max(self.mem_cost), 1)
        normalized_mem_cost = [x / max_req for x in self.mem_cost]
        if self.counter < 20:
            return None
        for runtime_id, node in enumerate(normalized_mem_cost):
            if node < 0.1:
                low_load_nodes.append(runtime_id)
        if not low_load_nodes:
            return None
        y = len(low_load_nodes)
        x = self.num_gpus - y
        for runtime_id in low_load_nodes:
            if np.random.rand() < y/(x + y): # Steal the node

                return runtime_id
        return None

    def update_overload_detector(self, input_ids, runtime_idx, func_output: RequestFuncOutput):
        # Overload detector based on the current ttft
        leaf_node = self.cache.find_node(input_ids)
        important_node = self.get_important_node(leaf_node)
        self.overload_detector.add_data_point(datetime.now(), important_node, runtime_idx, func_output.ttft)
    