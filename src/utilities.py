# -*- coding: utf-8 -*-
#
#    ICRAR - International Centre for Radio Astronomy Research
#    (c) UWA - The University of Western Australia
#    Copyright by UWA (in the framework of the ICRAR)
#    All rights reserved
#
#    This library is free software; you can redistribute it and/or
#    modify it under the terms of the GNU Lesser General Public
#    License as published by the Free Software Foundation; either
#    version 2.1 of the License, or (at your option) any later version.
#
#    This library is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#    Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public
#    License along with this library; if not, write to the Free Software
#    Foundation, Inc., 59 Temple Place, Suite 330, Boston,
#    MA 02111-1307  USA
#
"""

"""
from __future__ import print_function

import logging
import os
from os import makedirs
from os.path import exists
from timeit import default_timer

import h5py
import numpy as np
import pandas as pd
from astropy.utils.console import human_time
from statsmodels.robust import scale
from torch.utils.data import Dataset

from constants import NUMBER_CHANNELS, NUMBER_OF_CLASSES, H5_VERSION

LOGGER = logging.getLogger(__name__)


class H5Exception(Exception):
    pass


class RfiData(object):
    def __init__(self, **kwargs):
        self._sequence_length = kwargs['sequence_length']
        self._num_processes = kwargs['num_processes']
        self._using_gpu = kwargs['using_gpu']
        output_file = os.path.join(kwargs['data_path'], kwargs['data_file'])
        with h5py.File(output_file, 'r') as h5_file:
            data_group = h5_file['data']

            # Move the data into memory
            self._data_channel_0 = np.copy(data_group['data_channel_0'])
            self._labels = np.copy(data_group['labels'])

            length_data = len(self._labels) - kwargs['sequence_length']
            split_point1 = int(length_data * kwargs['training_percentage'] / 100.)
            split_point2 = int(length_data * (kwargs['training_percentage'] + kwargs['validation_percentage']) / 100.)
            perm0 = np.arange(length_data)
            np.random.shuffle(perm0)

            self._train_sequence = perm0[:split_point1]
            self._validation_sequence = perm0[split_point1:split_point2]
            self._test_sequence = perm0[split_point2:]

    def get_rfi_dataset(self, data_type, rank=None, short_run_size=None):
        if data_type not in ['training', 'validation', 'test']:
            raise ValueError("data_type must be one of: 'training', 'validation', 'test'")

        if data_type == 'training':
            sequence = self._train_sequence
        elif data_type == 'validation':
            sequence = self._validation_sequence
        else:
            sequence = self._test_sequence

        if self._using_gpu or rank is None:
            if short_run_size is not None:
                sequence = sequence[0:short_run_size]
        else:
            section_length = len(sequence) / self._num_processes
            start = rank * section_length
            if rank == self._num_processes - 1:
                if short_run_size is not None:
                    sequence = sequence[start:start + short_run_size]
                else:
                    sequence = sequence[start:]
            else:
                if short_run_size is not None:
                    sequence = sequence[start:start + short_run_size]
                else:
                    sequence = sequence[start:start + section_length]

        return RfiDataset(sequence, self._data_channel_0, self._labels, self._sequence_length)


class RfiDataset(Dataset):
    def __init__(self, selection_order, x_data, y_data, sequence_length):
        self._x_data = x_data
        self._y_data = y_data
        self._selection_order = selection_order
        self._length = len(selection_order)
        self._sequence_length = sequence_length
        self._actual_node = self._sequence_length / 2
        self._median = np.median(x_data)
        self._median_absolute_deviation = scale.mad(x_data, c=1)
        self._mean = np.mean(x_data)
        LOGGER.debug('Length: {}'.format(self._length))

    def __len__(self):
        return self._length

    def __getitem__(self, index):
        selection_index = self._selection_order[index]
        x_data = self._x_data[selection_index:selection_index + self._sequence_length]
        local_median = np.median(x_data)
        local_median_absolute_deviation = scale.mad(x_data, c=1)
        local_mean = np.mean(x_data)
        # x_data_last = x_data[self._actual_node]

        data = [self._median, self._median_absolute_deviation, self._mean, local_median, local_median_absolute_deviation, local_mean]
        for item in x_data:
            data.append(item)
            data.append(item - self._mean)
            data.append(item - self._median)
            data.append(item - self._median_absolute_deviation)
            data.append(item - local_mean)
            data.append(item - local_median)
            data.append(item - local_median_absolute_deviation)

        # return np.reshape(x_data, (NUMBER_CHANNELS, -1)), values, self._y_data[selection_index + self._actual_node]
        return np.array(data), self._y_data[selection_index + self._actual_node]


def process_files(filename, rfi_label):
    """ Process a file and return the data and the labels """
    files_to_process = []
    for ending in ['.txt', '_loc.txt']:
        complete_filename = filename + ending
        if os.path.exists(complete_filename):
            files_to_process.append(complete_filename)

    if len(files_to_process) != 2:
        LOGGER.error('The line counts do not match for: {0}'.format(filename))
        return

    # Load the files into numpy
    LOGGER.info('Loading: {}'.format(files_to_process[0]))
    data_frame = pd.read_csv(files_to_process[0], header=None, delimiter=' ')
    data = data_frame.values.flatten()

    LOGGER.info('Loading: {}'.format(files_to_process[1]))
    data_frame = pd.read_csv(files_to_process[1], header=None, delimiter=' ')
    labels = data_frame.values.flatten()

    # Check the lengths match
    assert len(data) == len(labels), 'The line counts do not match for: {0}'.format(filename)

    # If substitute of the label is needed
    if rfi_label != 1:
        labels[labels == 1] = rfi_label

    return data, labels


def build_data(**kwargs):
    """ Read data """
    output_file = os.path.join(kwargs['data_path'], kwargs['data_file'])
    if os.path.exists(output_file):
        with h5py.File(output_file, 'r') as h5_file:
            # Everything matches
            if 'version' in h5_file.attrs and h5_file.attrs['version'] == H5_VERSION:
                # All good nothing to do
                return

    # Open the output files
    with Timer('Processing input files'):
        data1, labels1 = process_files('../data/GMRT/impulsive_broadband_simulation_random_5p', 1)
        data2, labels2 = process_files('../data/GMRT/impulsive_broadband_simulation_random_10p', 1)
        data3, labels3 = process_files('../data/GMRT/repetitive_rfi_timeseries', 1)
        data4, labels4 = process_files('../data/GMRT/repetitive_rfi_random_timeseries', 1)
        # data0, labels0 = process_files('../data/GMRT/impulsive_broadband_simulation_random_norfi', 0)

    # Concatenate
    with Timer('Concatenating data'):
        labels = np.concatenate((labels1, labels2, labels3, labels4))
        data = np.concatenate((data1, data2, data3, data4))

    # Standardise and one hot
    with Timer('Standardise & One hot'):
        labels = one_hot(labels, NUMBER_OF_CLASSES)
        # data = normalize(data)

    with Timer('Saving to {0}'.format(output_file)):
        if not exists(kwargs['data_path']):
            makedirs(kwargs['data_path'])
        with h5py.File(output_file, 'w') as h5_file:
            h5_file.attrs['number_channels'] = NUMBER_CHANNELS
            h5_file.attrs['number_classes'] = NUMBER_OF_CLASSES
            h5_file.attrs['version'] = H5_VERSION

            data_group = h5_file.create_group('data')
            data_group.attrs['length_data'] = len(data)
            data_group.create_dataset('data_channel_0', data=data, compression='gzip')
            data_group.create_dataset('labels', data=labels, compression='gzip')


def get_h5_file(args):
    """ Read data """
    output_file = os.path.join(args.data_path, args.data_file)
    if os.path.exists(output_file):
        with Timer('Checking HDF5 file'):
            h5_file = h5py.File(output_file, 'r')
            # Everything matches
            if h5_file.attrs['validation_percentage'] == args.validation_percentage and h5_file.attrs['training_percentage'] == args.training_percentage:
                return h5_file

    # The read data needs to be called first
    raise H5Exception('You need to call build data first')


def normalize(all_data):
    """ normalize data """
    min_value = np.min(all_data)
    max_value = np.max(all_data)
    return (all_data - min_value) / (max_value - min_value)


def one_hot(labels, number_class):
    """ One-hot encoding """
    expansion = np.eye(number_class)
    y = expansion[:, labels].T
    assert y.shape[1] == number_class, "Wrong number of labels!"

    return y


class Timer(object):
    def __init__(self, name=None):
        self.name = '' if name is None else name
        self.timer = default_timer

    def __enter__(self):
        LOGGER.info('{}, Starting timer'.format(self.name))
        self.start = self.timer()
        return self

    def __exit__(self, *args):
        end = self.timer()
        self.elapsed_secs = end - self.start
        self.elapsed = self.elapsed_secs
        LOGGER.info('{}, Elapsed time: {}'.format(self.name, human_time(self.elapsed)))
