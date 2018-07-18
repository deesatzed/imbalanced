# -*- coding: utf-8 -*-

from typing import Dict, Tuple, List, Union, Any, Optional
import numpy as np
import torch
from torch.utils.data import Dataset


class DatasetWrapper(Dataset):
    """Base class for datasets that result from wrapping another Dataset object.
    """

    def __init__(self, dataset: Dataset,
                 lock_dataset: bool = True) -> None:
        """Create a DatasetWrapper object.

        Args:
            dataset:      The dataset to be wrapped.
            lock_dataset: Whether the wrapped dataset can be updated after
                          the wrapper object is constructed.  Typically set
                          to True, since doing so is likely to break indexing
                          used by the wrapper.
        """
        # Init
        self._dataset = None
        # Set
        self.dataset = dataset
        self._lock_dataset = True
        if isinstance(lock_dataset, bool):
            self._lock_dataset = lock_dataset

    @property
    def dataset(self) -> Dataset:
        """Get the wrapped dataset.

        Returns:
            The wrapped dataset.

        """
        return self._dataset

    @dataset.setter
    def dataset(self, dataset) -> None:
        """Set the wrapped dataset.

        Args:
            dataset: The dataset to be wrapped.

        """
        if self._dataset is not None:
            assert not self._lock_dataset,\
                'Wrapped dataset cannot be changed ' \
                'after wrapper has been initialized.'
        assert isinstance(dataset, Dataset),\
            'Wrapped object must be an instance of Dataset (or a subclass).'
        self._dataset = dataset


class PartitionedDataset(DatasetWrapper):
    """Partitioned dataset wrapper.

    Allows for e.g. partitioning a given dataset into train/val/test splits.
    """

    def __init__(
        self,
        dataset: Dataset,
        partitions: Optional[Dict[str, Union[int, float]]] = None
    ) -> None:
        """Create a PartitionedDataset object.

        :param dataset:     the dataset to be partitioned
        :type  dataset:     Dataset
        :param partitions:  a dictionary specifying the partitions and their
                            respective sizes, or None for a sensible default
        :type  partitions:  dict or None
        """

        super().__init__(dataset)

        # Reasonable defaults
        # Equivalent to 80:20 split twice
        if partitions is None:
            partitions = {'train': 0.64, 'val': 0.16, 'test': 0.2}

        # Basic validation
        assert isinstance(partitions, dict),\
            'Partition specification must be a dictionary object'
        assert len(partitions) >= 2, \
            'Partition specification must define at least two partitions'
        assert min(partitions.values()) > 0, \
            'Partitions must be non-empty'

        # Convert fractional partitions to integer sizes
        if sum(list(partitions.values())) <= 1:
            partitions = {name: round(fraction * len(dataset))
                          for name, fraction in partitions.items()}

        # Further validation
        for size in partitions.values():
            assert size == int(size),\
                'Partition sizes must be integer, or fractional and sum to <= 1'
        assert sum(partitions.values()) == len(dataset),\
            'Partition sizes must sum to dataset size'

        # Cache useful properties
        self._partition_names = list(partitions.keys())
        self._partition_sizes = [int(partitions[name])
                                 for name in self._partition_names]
        self._partition_offsets = np.cumsum(self._partition_sizes)

        # Set active partition
        self._active_partition_idx = None
        if 'train' in self._partition_names:
            # Set train as default, if it's there
            self.set_active_partition('train')
        else:
            # Just use the first partition
            self.set_active_partition(self._partition_names[0])

    @property
    def active_partition(self) -> str:
        """Get the currently active partition.

        Returns:
            The name of the active partition.

        """
        return self._partition_names[self._active_partition_idx]

    def set_active_partition(self, partition: str) -> None:
        """Set the active partition.

        Args:
            partition: The name of the desired partition.

        """
        assert partition in self._partition_names,\
            'Invalid partition specified'
        self._active_partition_idx = self._partition_names.index(partition)

    @property
    def partitions(self) -> Dict[str, int]:
        """Get the partitions in original dict format.

        Returns:
            The dictionary of partitions.
        """
        return {name: self._partition_sizes[i]
                for i, name in enumerate(self._partition_names)}

    @property
    def train(self) -> 'PartitionedDataset':
        """Shortcut to get the 'train' partition."""
        self.set_active_partition('train')
        return self

    @property
    def val(self) -> 'PartitionedDataset':
        """Shortcut to get the 'val' partition."""
        self.set_active_partition('val')
        return self

    @property
    def test(self) -> 'PartitionedDataset':
        """Shortcut to get the 'test' partition."""
        self.set_active_partition('test')
        return self

    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, torch.Tensor]:
        """Get a data row by index.

        Args:
            idx: The index of the desired row.

        Returns:
            The desired row (input, target)

        """
        if idx >= self._partition_sizes[self._active_partition_idx]:
            raise IndexError()
        offset = self._partition_offsets[self._active_partition_idx - 1]
        return self.dataset[offset + idx]

    def __len__(self) -> int:
        """Get the total number of rows in the dataset.

        Returns:
            The number of rows.

        """
        return self._partition_sizes[self._active_partition_idx]

    @property
    def c_args(self) -> List[Tuple[str, Any]]:
        """Get the canonical (ordered) list of arguments ('c-args') which define
        the current object.

        Returns:
            The arguments, as a list of tuples (arg_name, arg_value).

        """
        return [
            ('dataset', self.dataset),
            ('partitions', self.partitions)
        ]


class ResampledDataset(DatasetWrapper):
    """Resampled dataset.

    Represents a resampled version of a given dataset.

    Wraps but does not copy the original dataset.  Instead stores only the
    minimum representation of the *differences* between the original dataset
    and the resampled one.

    For simple resampling, a list containing the indices of those rows
    which are 'active' in the resample is maintained.  Thus, rows from the
    original dataset may be:

         - Deleted from the resampled data, by removing their index from
           the aforementioned active rows list.

         - Re-introduced, by adding the removed index once more.

         - Duplicated (as in simple over-sampling), by adding duplicates of
           a row's index to the active list.

    This form of row-index structure is considered appropriate due to the
    typical sparsity pattern of resampled imbalanced data.

    Note that a combination of synthetic generation and simple sampling can be
    achieved by using this wrapper on a ConcatenatedDataset consisting of the
    original and the synthetically generated samples.  See the pre-processors
    module for more information.
    """

    def __init__(self, dataset: Dataset,
                 samples: Optional[np.ndarray] = None) -> None:
        """Create a ResampledDataset object.

        Args:
            dataset: The original dataset being resampled.
            samples: The indices of rows that have been resampled from the
                     original dataset.  Duplicates allowed. If None is passed,
                     all rows in the original dataset will be used.
        """
        super().__init__(dataset)
        self._samples = None
        if samples is not None:
            self.samples = samples
        else:
            # Default to entire dataset
            self.samples = np.arange(len(self.dataset), dtype=np.int)

    @property
    def samples(self) -> np.ndarray:
        """Get the sampled indices of the original dataset.

        Returns:
            A numpy array of the sampled indices.

        """
        return self._samples

    @samples.setter
    def samples(self, samples: np.ndarray) -> None:
        """Set the sampled indices of the original dataset.

        Args:
            samples: The desired sample indices.

        """
        # First make sure it's a numpy array
        assert isinstance(samples, np.ndarray),\
            'Sample indices must be a numpy ndarray'
        # Squash it
        samples = samples.flatten()
        # More checks
        # todo: assert integer?
        assert samples.min() >= 0,\
            'Sample indices must be non-negative'
        assert samples.max() < len(self.dataset),\
            'Sample indices must be found in original dataset'
        # Set it
        self._samples = samples

    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, torch.Tensor]:
        """Get a data row by index.

        Args:
            idx: The index of the desired row.

        Returns:
            The desired row (input, target)

        """
        return self.dataset[self._samples[idx]]

    def __len__(self) -> int:
        """Get the total number of rows in the dataset.

        Returns:
            The number of rows.

        """
        return len(self._samples)

    @property
    def c_args(self) -> List[Tuple[str, Any]]:
        """Get the canonical (ordered) list of arguments ('c-args') which define
        the current object.

        Returns:
            The arguments, as a list of tuples (arg_name, arg_value).

        """
        return [
            ('dataset', self.dataset),
            ('samples', self.samples)
        ]
