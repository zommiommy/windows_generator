

import os
import time
import random
import itertools
import numpy as np
import pandas as pd
from math import ceil
from tqdm.auto import tqdm
from ucsc_genomes_downloader import Genome
from multiprocessing import Pool, cpu_count

from .decorators import cache_method

# Dataset generator Batchsize_scheduler
#       \                 /
#         \             /
#            \        /
#           Buffer Generator
#                  |
#           _buffer_encoder_generator
#                  |
#           _generator
#


class WindowsGenerator:

    n_types = ["uniform", "normal"]

    def __init__(self, 
            assembly,
            window_size,
            batch_size,
            buffer_size=None,
            max_gap_size=100,
            test_chromosomes = [],
            cache_dir=None,
            lazy_load=True,
            n_type="uniform"
            ):
        self.assembly, self.window_size = assembly, window_size
        self.max_gap_size, self.batch_size, self.test_chromosomes = max_gap_size, batch_size, test_chromosomes

        # Buffersize default None == cpu count for optimal performance:
        if not buffer_size:
            buffer_size = cpu_count()
        self.buffer_size = buffer_size

        # Validate the type of N
        if n_type not in self.n_types:
            raise ValueError("n_type must be one of %s"%n_type)
        self.n_type = n_type

        # Get the cache dir
        cache_dir = cache_dir or os.environ.get("CACHE_PATH", None) or "/tmp"

        self._cache_directory = "/".join([cache_dir, assembly, str(window_size)]) 

        # Generate a pool of processes to save the overhead
        self.workers = cpu_count()
        self.pool = Pool(self.workers)

        # Preprocess all the possible data
        self.genome = Genome(
            assembly=assembly,
            lazy_load=lazy_load,
            cache_directory=cache_dir,
        )

        self.chromosomes = sorted(list(self.genome))

        assert all((x in self.chromosomes for x in test_chromosomes)), "the chromosomes inside of test_chromosomes must be in {}".format(self.chromosomes)

        filled = self.genome.filled()
        windows = self._tasselize_windows(filled, window_size)
        sequences = self._encode_sequences(windows)

        self._windows_train, self._windows_test = self._train_test_split(sequences)

        gap_mask = self._render_gaps()
        self._mean, self._cov = self._model_gaps(gap_mask)

    def _train_test_split(self, sequences):
        # Get the set of chromosomes
        # TODO do we need a seed here?
        # Find the splitting index  
        windows_train = sum(
            [
                sequences[chrom].sequence.tolist()
                for chrom in tqdm(
                    self.chromosomes,
                    desc="Gropping Train windows",
                    leave=False
                )
                if chrom not in self.test_chromosomes
            ]
            , []
        )
        windows_test = sum(
            (
                sequences[chrom].sequence.tolist()
                for chrom in tqdm(
                    self.chromosomes,
                    desc="Gropping Test windows",
                    leave=False
                )
                if chrom in self.test_chromosomes
            )
            , []
        )
        return windows_train, windows_test

    def __len__(self):
        return len(self._windows_train) // self.batch_size

    @cache_method("{_cache_directory}/gap_mask{max_gap_size}.pkl")
    def _render_gaps(self):
        # Compute
        gaps = self.genome.gaps()
        # Keeping only small gaps
        gaps = gaps[gaps.chromEnd - gaps.chromStart <= self.max_gap_size]
        # Expand windows
        mid_point = ((gaps.chromEnd + gaps.chromStart)/2).round().astype(int)
        gaps.chromStart = (mid_point - self.window_size/2).round().astype(int)
        gaps.chromEnd = (mid_point + self.window_size/2).round().astype(int)
        # Rendering gap sequences
        gapped_sequences = self.genome.bed_to_sequence(gaps)
        # Rendering gap mask
        return np.array([
            np.array(list(sequence.lower())) == "n"
            for sequence in gapped_sequences.sequence
        ])

    def _model_gaps(self, gap_mask):
        return np.mean(gap_mask, axis=0), np.cov(gap_mask.T),


    @cache_method("{_cache_directory}/tasselized.pkl")
    def _tasselize_windows(self, bed:pd.DataFrame, window_size:int):
        # Compute
        tasks = (
            (row.chrom, row.chromStart, row.chromEnd, window_size)
            for _, row in bed.iterrows()
        )
        return pd.concat(list(tqdm(
            self.pool.imap(tasselize_window, tasks),
            total=bed.shape[0],
            desc="Tasselizing windows",
            leave=False
        )))

    @cache_method("{_cache_directory}/encoded_seq_{max_gap_size}_{chrom}.pkl")
    def _parse_sequence(self, windows, chrom):
        return self.genome.bed_to_sequence(windows)
        
    def _encode_sequences(self, windows):
        return {
            chrom:self._parse_sequence(window, chrom=chrom)
            for chrom, window in tqdm(
                windows.groupby("chrom"),
                desc="Loading the sequences for chromosomes",
                leave=False
            )
        }

    def batchsize_scheduler(self):
        while True:
            yield self.batch_size

    def _dataset_generator(self, dataset):
        while True:
            np.random.shuffle(dataset)
            for value in dataset:
                yield value


    def _buffer_generator(self, dataset):
        iterable = self._dataset_generator(dataset)
        for batch_size in self.batchsize_scheduler():
            yield [
                list(itertools.islice(iterable, batch_size))
                for _ in range(self.buffer_size)
            ]

    def _buffer_encoder_generator(self, dataset):
        for buffer in self._buffer_generator(dataset):
            yield list(self.pool.imap(one_hot_encoder, buffer))

    def _generator(self, dataset):
        try:
            for buffer in self._buffer_encoder_generator(dataset):
                for batch in buffer:
                    yield batch
        except KeyboardInterrupt:
            self.pool.terminate()
            self.pool.join()

    def train(self):
        return self._generator(self._windows_train)
        
    def test(self):
        if not self.test_chromosomes: 
            raise ValueError(
                "Can't return the test generator since "
                "no test chromosomes were specified"
            )
        return self._generator(self._windows_test)