import cv2
import numpy as np
from config import cfg
import os
import glob
from sklearn.utils import shuffle
from utils.check_data import check_preprocessed_data, get_file_names
import net.processing.boxes3d  as box
from multiprocessing import Lock, Process,Queue as Queue, Value,Array, cpu_count
import queue
import time

import config
import os
import numpy as np
import glob
import cv2
from kitti_data.pykitti.tracklet import parseXML, TRUNC_IN_IMAGE, TRUNC_TRUNCATED
from config import cfg
import data
import net.utility.draw as draw
from raw_data import *
from utils.training_validation_data_splitter import TrainingValDataSplitter
import pickle
import array
import data
from sklearn.utils import shuffle
import threading
import scipy.io 
from net.processing.boxes3d import *
import math
import random
import sys

from net.utility.front_top_preprocess import lidar_to_top_cuda, lidar_to_front_cuda

# disable print
# import sys
# f = open(os.devnull, 'w')
# sys.stdout = f

def load(file_names, is_testset=False):
    # here the file names is like /home/stu/round12_data_out_range/preprocessed/didi/top/2/14_f/00013, the top inside
    first_item = file_names[0].split('/')
    prefix = '/'.join(first_item[:-4])
    #  need to be replaced.
    frame_num_list = ['/'.join(name.split('/')[-3:]) for name in file_names]

    # print('rgb path here: ', os.path.join(prefix,'rgb', date, driver, file + '.png'))
    train_rgbs = [cv2.imread(os.path.join(prefix, 'rgb', file + '.png'), 1) for file in frame_num_list]
    train_tops = [np.load(os.path.join(prefix, 'top', file + '.npy.npz'))['top_view'] for file in frame_num_list]
    train_fronts = [np.zeros((1, 1), dtype=np.float32) for file in frame_num_list]

    if is_testset == True:
        train_gt_boxes3d = None
        train_gt_labels = None
    else:
        train_gt_boxes3d = [np.load(os.path.join(prefix, 'gt_boxes3d', file + '.npy')) for file in frame_num_list]

        train_gt_labels = [np.load(os.path.join(prefix, 'gt_labels', file + '.npy')) for file in
                           frame_num_list]

    return train_rgbs, train_tops, train_fronts, train_gt_labels, train_gt_boxes3d


class batch_loading:
    def __init__(self, dir_path, dates_to_drivers=None, indice=None, cache_num=10, is_testset=False):
        self.dates_to_drivers = dates_to_drivers
        self.indice = indice
        self.cache_num = cache_num
        self.preprocess_path = dir_path
        self.is_testset = is_testset

        self.preprocess = data.Preprocess()
        self.raw_img = Image()
        self.raw_tracklet = Tracklet()
        self.raw_lidar = Lidar()

        # load_file_names is like 1_15_1490991691546439436 for didi or 2012_09_26_0005_00001 for kitti.
        if indice is None:
            self.load_file_names = self.get_all_load_index(self.preprocess_path, self.dates_to_drivers, is_testset)
            self.tags = self.raw_img.get_tags()
        else:
            # self.load_file_names = indice
            self.load_file_names = self.get_specific_load_index(indice, self.preprocess_path, self.dates_to_drivers,
                                                                is_testset)
            self.load_once = True
        self.size = len(self.tags)

        # self.shuffled_file_names = shuffle(self.load_tags, random_state=1)
        # for getting current index in shuffled_file_names
        self.batch_start_index = 0

        # num_frame_used means how many frames are used in current batch, if all frame are used, load another batch
        self.num_frame_used = cache_num

        # current batch contents
        self.train_rgbs = []
        self.train_tops = []
        self.train_fronts = []
        self.train_gt_labels = []
        self.train_gt_boxes3d = []
        self.current_batch_file_names = []

    def load_from_one_tag(self, one_frame_tag):
        obstacles = self.raw_tracklet.load(one_frame_tag)
        rgb = self.raw_img.load(one_frame_tag)
        lidar = self.raw_lidar.load(one_frame_tag)
        return obstacles, rgb, lidar

    def preprocess(self, rgb, lidar, obstacles):
        rgb = preprocess.rgb(rgb)
        top = lidar_to_top_cuda(lidar)
        boxes3d = [preprocess.bbox3d(obs) for obs in obstacles]
        labels = [preprocess.label(obs) for obs in obstacles]
        return rgb, top, boxes3d, labels

    def draw_bbox_on_rgb(self, rgb, boxes3d):
        img = draw.draw_box3d_on_camera(rgb, boxes3d)
        new_size = (img.shape[1] // 3, img.shape[0] // 3)
        img = cv2.resize(img, new_size)
        path = os.path.join(config.cfg.LOG_DIR, 'test', 'rgb', '%s.png' % one_frame_tag.replace('/', '_'))
        cv2.imwrite(path, img)
        print('write %s finished' % path)

    def draw_bbox_on_lidar_top(self, top, boxes3d):
        path = os.path.join(config.cfg.LOG_DIR, 'test', 'top', '%s.png' % one_frame_tag.replace('/', '_'))
        top_image = data.draw_top_image(top)
        top_image = data.draw_box3d_on_top(top_image, boxes3d, color=(0, 0, 80))
        cv2.imwrite(path, top_image)
        print('write %s finished' % path)

    def get_shape(self):

        # print("file name is here: ", self.load_file_names[0])
        train_rgbs, train_tops, train_fronts, train_gt_labels, train_gt_boxes3d = load([self.load_file_names[0]],
                                                                                       is_testset=self.is_testset)

        obstacles, rgb, lidar = self.load_from_one_tag([self.tags[0]],
                                                       is_testset=self.is_testset)
        train_rgbs, train_tops, train_fronts, train_gt_labels, train_gt_boxes3d = self.preprocess()

        top_shape = train_tops[0].shape
        front_shape = train_fronts[0].shape
        rgb_shape = train_rgbs[0].shape

        return top_shape, front_shape, rgb_shape

    def get_all_load_index(self, data_seg, dates_to_drivers, gt_included):
        # check if input data (rgb, top, gt_labels, gt_boxes) have the same amount.
        check_preprocessed_data(data_seg, dates_to_drivers, gt_included)
        top_dir = os.path.join(data_seg, "top")
        # print('lidar data here: ', lidar_dir)
        load_indexs = []
        for date, drivers in dates_to_drivers.items():
            for driver in drivers:
                # file_prefix is something like /home/stu/data/preprocessed/didi/lidar/2011_09_26_0001_*
                file_prefix = os.path.join(data_seg, "top", date, driver)
                driver_files = get_file_names(data_seg, "top", driver, date)
                if len(driver_files) == 0:
                    raise ValueError('Directory has no data starts from {}, please revise.'.format(file_prefix))

                name_list = [file.split('/')[-1].split('.')[0] for file in driver_files]
                name_list = [file.split('.')[0] for file in driver_files]
                load_indexs += name_list
        load_indexs = sorted(load_indexs)
        return load_indexs

    def get_specific_load_index(self, index, data_seg, dates_to_drivers, gt_included):
        # check if input data (rgb, top, gt_labels, gt_boxes) have the same amount.
        check_preprocessed_data(data_seg, dates_to_drivers, gt_included)
        top_dir = os.path.join(data_seg, "top")
        # print('lidar data here: ', lidar_dir)
        load_indexs = []
        for date, drivers in dates_to_drivers.items():
            for driver in drivers:
                # file_prefix is something like /home/stu/data/preprocessed/didi/lidar/2011_09_26_0001_*
                file_prefix = os.path.join(data_seg, "top", driver, date)
                driver_files = get_file_names(data_seg, "top", driver, date, index)
                if len(driver_files) == 0:
                    raise ValueError('Directory has no data starts from {}, please revise.'.format(file_prefix))

                name_list = [file.split('/')[-1].split('.')[0] for file in driver_files]
                name_list = [file.split('.')[0] for file in driver_files]
                load_indexs += name_list
        load_indexs = sorted(load_indexs)
        return load_indexs

    def load_test_frames(self, size, shuffled):
        # just load it once
        if self.load_once:
            if shuffled:
                self.load_file_names = shuffle(self.load_file_names)
            self.train_rgbs, self.train_tops, self.train_fronts, self.train_gt_labels, self.train_gt_boxes3d = \
                load(self.load_file_names)
            self.num_frame_used = 0
            self.load_once = False
        # if there are still frames left
        self.current_batch_file_names = self.load_file_names
        frame_end = min(self.num_frame_used + size, self.cache_num)
        train_rgbs = self.train_rgbs[self.num_frame_used:frame_end]
        train_tops = self.train_tops[self.num_frame_used:frame_end]
        train_fronts = self.train_fronts[self.num_frame_used:frame_end]
        train_gt_labels = self.train_gt_labels[self.num_frame_used:frame_end]
        train_gt_boxes3d = self.train_gt_boxes3d[self.num_frame_used:frame_end]
        handle_id = self.current_batch_file_names[self.num_frame_used:frame_end]
        handle_id = ['/'.join(name.split('/')[-3:]) for name in handle_id]
        # print("start index is here: ", self.num_frame_used)
        self.num_frame_used = frame_end
        if self.num_frame_used >= self.size:
            self.num_frame_used = 0
        # return number of batches according to current size.
        return train_rgbs, train_tops, train_fronts, train_gt_labels, train_gt_boxes3d, handle_id

    # size is for loading how many frames per time.
    def load_batch(self, size, shuffled):
        if shuffled:
            self.load_file_names = shuffle(self.load_file_names)

        # if all frames are used up, reload another batch according to cache_num
        if self.num_frame_used >= self.cache_num:
            batch_end_index = self.batch_start_index + self.cache_num

            if batch_end_index < self.size:
                loaded_file_names = self.load_file_names[self.batch_start_index:batch_end_index]
                self.batch_start_index = batch_end_index

            else:
                # print("end of the data is here: ", self.batch_start_index)
                diff_to_end = self.size - self.batch_start_index
                start_offset = self.cache_num - diff_to_end

                file_names_to_end = self.load_file_names[self.batch_start_index:self.size]
                if shuffled:
                    self.load_file_names = shuffle(self.load_file_names)

                file_names_from_start = self.load_file_names[0:start_offset]

                loaded_file_names = file_names_to_end + file_names_from_start
                self.batch_start_index = start_offset
                # print("after reloop: ", self.batch_start_index)

            # print('The loaded file name here: ', loaded_file_names)
            self.current_batch_file_names = loaded_file_names
            self.train_rgbs, self.train_tops, self.train_fronts, self.train_gt_labels, self.train_gt_boxes3d = \
                load(loaded_file_names, is_testset=self.is_testset)
            self.num_frame_used = 0

        # if there are still frames left
        frame_end = min(self.num_frame_used + size, self.cache_num)
        train_rgbs = self.train_rgbs[self.num_frame_used:frame_end]
        train_tops = self.train_tops[self.num_frame_used:frame_end]
        train_fronts = self.train_fronts[self.num_frame_used:frame_end]
        if self.is_testset:
            train_gt_labels = None
            train_gt_boxes3d = None
        else:
            train_gt_labels = self.train_gt_labels[self.num_frame_used:frame_end]
            train_gt_boxes3d = self.train_gt_boxes3d[self.num_frame_used:frame_end]
        # print("start index is here: ", self.num_frame_used)
        handle_id = self.current_batch_file_names[self.num_frame_used:frame_end]
        handle_id = ['/'.join(name.split('/')[-3:]) for name in handle_id]
        # print('handle id here: ', handle_id)
        self.num_frame_used = frame_end
        # return number of batches according to current size.
        return train_rgbs, train_tops, train_fronts, train_gt_labels, train_gt_boxes3d, handle_id

    def get_date_and_driver(self, handle_id):
        date_n_driver = ['/'.join(item.split('/')[0:2]) for item in handle_id]
        return date_n_driver

    def get_frame_info(self, handle_id):
        return handle_id

    def keep_gt_inside_range(self, train_gt_labels, train_gt_boxes3d):
        # todo : support batch size >1
        if train_gt_labels.shape[0] == 0:
            return False, None, None
        assert train_gt_labels.shape[0] == train_gt_boxes3d.shape[0]

        # get limited train_gt_boxes3d and train_gt_labels.
        keep = np.zeros((len(train_gt_labels)), dtype=bool)

        for i in range(len(train_gt_labels)):
            # DontCare object(-1,-1,-1) are dropped in this step
            if box.box3d_in_top_view(train_gt_boxes3d[i]):
                keep[i] = 1

        # if all targets are out of range in selected top view, return True.
        if np.sum(keep) == 0:
            return False, None, None

        train_gt_labels = train_gt_labels[keep]
        train_gt_boxes3d = train_gt_boxes3d[keep]
        return True, train_gt_labels, train_gt_boxes3d

    def load(self, size, batch=True, shuffled=False):
        load_frames = True
        while load_frames:
            if batch:
                train_rgbs, train_tops, train_fronts, train_gt_labels, train_gt_boxes3d, frame_id = self.load_batch(size,
                                                                                                              shuffled)
            else:
                train_rgbs, train_tops, train_fronts, train_gt_labels, train_gt_boxes3d, frame_id = \
                    self.load_test_frames(size, shuffled)
            load_frames = False

            if not self.is_testset:
                # for keeping all gt labels and gt boxes inside range, and discard gt out of selected range.
                is_gt_inside_range, batch_gt_labels_in_range, batch_gt_boxes3d_in_range = \
                    self.keep_gt_inside_range(train_gt_labels[0], train_gt_boxes3d[0])

                if not is_gt_inside_range:
                    load_frames = True
                    continue

                # modify gt_labels and gt_boxes3d values to be inside range.
                # todo current support only batch_size == 1lo
                train_gt_labels = np.zeros((1, batch_gt_labels_in_range.shape[0]), dtype=np.int32)
                train_gt_boxes3d = np.zeros((1, batch_gt_labels_in_range.shape[0], 8, 3), dtype=np.float32)
                train_gt_labels[0] = batch_gt_labels_in_range
                train_gt_boxes3d[0] = batch_gt_boxes3d_in_range

        return np.array(train_rgbs), np.array(train_tops), np.array(train_fronts), np.array(train_gt_labels), \
               np.array(train_gt_boxes3d), frame_id


def draw_bbox_on_rgb(rgb, boxes3d, one_frame_tag):
    img = draw.draw_box3d_on_camera(rgb, boxes3d)
    new_size = (img.shape[1] // 3, img.shape[0] // 3)
    img = cv2.resize(img, new_size)
    path = os.path.join(config.cfg.LOG_DIR, 'test', 'rgb', '%s.png' % one_frame_tag.replace('/', '_'))
    cv2.imwrite(path, img)
    print('write %s finished' % path)


def draw_bbox_on_lidar_top(top, boxes3d, one_frame_tag):
    path = os.path.join(config.cfg.LOG_DIR, 'test', 'top', '%s.png' % one_frame_tag.replace('/', '_'))
    top_image = data.draw_top_image(top)
    top_image = data.draw_box3d_on_top(top_image, boxes3d, color=(0, 0, 80))
    cv2.imwrite(path, top_image)
    print('write %s finished' % path)


use_thread = True

class BatchLoading2:

    def __init__(self, bags=[], tags=[], queue_size=20, require_shuffle=False,
                 require_log=False, is_testset=False):
        self.is_testset = is_testset
        self.shuffled = require_shuffle
        self.preprocess = data.Preprocess()
        self.raw_img = Image()
        self.raw_tracklet = Tracklet()
        self.raw_lidar = Lidar()

        self.bags = bags
        # get all tags
        self.tags = tags

        if self.shuffled:
            self.tags = shuffle(self.tags)

        self.tag_index = 0
        self.size = len(self.tags)

        self.require_log = require_log

        self.cache_size = queue_size
        self.loader_need_exit = Value('i', 0)

        if use_thread:
            self.prepr_data=[]
            self.lodaer_processing = threading.Thread(target=self.loader)
        else:
            self.preproc_data_queue = Queue()
            self.buffer_blocks = [Array('h', 41246691) for i in range(queue_size)]
            self.blocks_usage = Array('i', range(queue_size))
            self.lodaer_processing = Process(target=self.loader)
        self.lodaer_processing.start()


    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.loader_need_exit.value=True
        if self.require_log: print('set loader_need_exit True')
        self.lodaer_processing.join()
        if self.require_log: print('exit lodaer_processing')

    def keep_gt_inside_range(self, train_gt_labels, train_gt_boxes3d):
        train_gt_labels = np.array(train_gt_labels, dtype=np.int32)
        train_gt_boxes3d = np.array(train_gt_boxes3d, dtype=np.float32)
        if train_gt_labels.shape[0] == 0:
            return False, None, None
        assert train_gt_labels.shape[0] == train_gt_boxes3d.shape[0]

        # get limited train_gt_boxes3d and train_gt_labels.
        keep = np.zeros((len(train_gt_labels)), dtype=bool)

        for i in range(len(train_gt_labels)):
            if box.box3d_in_top_view(train_gt_boxes3d[i]):
                keep[i] = 1

        # if all targets are out of range in selected top view, return True.
        if np.sum(keep) == 0:
            return False, None, None

        train_gt_labels = train_gt_labels[keep]
        train_gt_boxes3d = train_gt_boxes3d[keep]
        return True, train_gt_labels, train_gt_boxes3d

    def load_from_one_tag(self, one_frame_tag):
        if self.is_testset:
            obstacles = None
        else:
            obstacles = self.raw_tracklet.load(one_frame_tag)
        rgb = self.raw_img.load(one_frame_tag)
        lidar = self.raw_lidar.load(one_frame_tag)
        return obstacles, rgb, lidar


    def preprocess_one_frame(self, rgb, lidar, obstacles):
        rgb = self.preprocess.rgb(rgb)
        top = lidar_to_top_cuda(lidar)
        front = lidar_to_front_cuda(lidar)

        if self.is_testset:
            return rgb, top, None, None
        boxes3d = [self.preprocess.bbox3d(obs) for obs in obstacles]
        labels = [self.preprocess.label(obs) for obs in obstacles]
        return rgb, top, boxes3d, labels

    def get_shape(self):
        train_rgbs, train_tops, train_fronts, train_gt_labels, train_gt_boxes3d, _ = self.load()
        top_shape = train_tops[0].shape
        front_shape = train_fronts[0].shape
        rgb_shape = train_rgbs[0].shape

        return top_shape, front_shape, rgb_shape

    def data_preprocessed(self):
        # only feed in frames with ground truth labels and bboxes during training, or the training nets will break.
        skip_frames = True
        while skip_frames:
            #fronts = []
            frame_tag = self.tags[self.tag_index]
            obstacles, rgb, lidar = self.load_from_one_tag(frame_tag)
            rgb, top, boxes3d, labels, fronts = self.preprocess_one_frame(rgb, lidar, obstacles)
            if self.require_log and not self.is_testset:
                draw_bbox_on_rgb(rgb, boxes3d, frame_tag)
                draw_bbox_on_lidar_top(top, boxes3d, frame_tag)

            # reset self tag_index to 0 and shuffle tag list
            if self.tag_index >= self.size:
                self.tag_index = 0
                if self.shuffled:
                    self.tags = shuffle(self.tags)
            skip_frames = False

            # only feed in frames with ground truth labels and bboxes during training, or the training nets will break.
            if not self.is_testset:
                is_gt_inside_range, batch_gt_labels_in_range, batch_gt_boxes3d_in_range = \
                    self.keep_gt_inside_range(labels, boxes3d)
                labels = batch_gt_labels_in_range
                boxes3d = batch_gt_boxes3d_in_range
                # if no gt labels inside defined range, discard this training frame.
                if not is_gt_inside_range:
                    skip_frames = True

        return np.array([rgb]), np.array([top]), np.array([fronts]), np.array([labels]), \
               np.array([boxes3d]), frame_tag

    def find_empty_block(self):
        idx = -1
        for i in range(self.cache_size):
            if self.blocks_usage[i] == 1:
                continue
            else:
                idx = i
                break
        return idx


    def loader(self):
        if use_thread:
            while self.loader_need_exit.value == 0:

                if len(self.prepr_data) >=self.cache_size:
                    time.sleep(1)
                    # print('sleep ')
                else:
                    self.prepr_data = [(self.data_preprocessed())]+self.prepr_data
                    # print('data_preprocessed')
        else:
            while self.loader_need_exit.value == 0:
                empty_idx = self.find_empty_block()
                if empty_idx == -1:
                    time.sleep(1)
                    # print('sleep ')
                else:
                    prepr_data = (self.data_preprocessed())
                    # print('data_preprocessed')
                    dumps = pickle.dumps(prepr_data)
                    length = len(dumps)
                    self.buffer_blocks[empty_idx][0:length] = dumps[0:length]

                    self.preproc_data_queue.put({
                        'index': empty_idx,
                        'length': length
                    })


        if self.require_log:print('loader exit')



    def load(self):
        if use_thread:
            while len(self.prepr_data)==0:
                time.sleep(1)
            data_ori = self.prepr_data.pop()


        else:

            # print('self.preproc_data_queue.qsize() = ', self.preproc_data_queue.qsize())
            info = self.preproc_data_queue.get(block=True)
            length = info['length']
            block_index = info['index']
            dumps = self.buffer_blocks[block_index][0:length]

            #set flag
            self.blocks_usage[block_index] = 0

            # convert to bytes string
            dumps = array.array('B',dumps).tostring()
            data_ori = pickle.loads(dumps)

        return data_ori



    def get_frame_info(self):
        return self.tags[self.tag_index]

# for non-raw dataset
class KittiLoading(object):

    def __init__(self, object_dir='.', queue_size=20, require_shuffle=False, is_testset=True, batch_size=1, use_precal_view=False, use_multi_process_num=0, split_file=''):
        assert(use_multi_process_num > 0)
        self.object_dir = object_dir
        self.is_testset, self.require_shuffle, self.use_precal_view = is_testset, require_shuffle, use_precal_view
        self.use_multi_process_num = use_multi_process_num if not self.is_testset else 1
        self.require_shuffle = require_shuffle if not self.is_testset else False
        self.batch_size=batch_size
        self.split_file = split_file 

        if self.split_file != '':
            # use split file  
            _tag = []
            self.f_rgb, self.f_lidar, self.f_top, self.f_front, self.f_label = [], [], [], [], []
            for line in open(self.split_file, 'r').readlines():
                line = line[:-1] # remove '\n'
                _tag.append(line)
                self.f_rgb.append(os.path.join(self.object_dir, 'training', 'image_2', line+'.png'))
                self.f_lidar.append(os.path.join(self.object_dir, 'training', 'velodyne', line+'.bin'))
                self.f_top.append(os.path.join(self.object_dir, 'training', 'top_view', line+'.npy'))
                self.f_front.append(os.path.join(self.object_dir, 'training', 'front_view', line+'.npy'))
                self.f_label.append(os.path.join(self.object_dir, 'training', 'label_2', line+'.txt'))
        else:
            self.f_rgb = glob.glob(os.path.join(self.object_dir, 'training', 'image_2', '*.png'))
            self.f_rgb.sort()
            self.f_lidar = glob.glob(os.path.join(self.object_dir, 'training', 'velodyne', '*.bin'))
            self.f_lidar.sort()
            self.f_top = glob.glob(os.path.join(self.object_dir, 'training', 'top_view', '*.npy'))
            self.f_top.sort()
            self.f_front = glob.glob(os.path.join(self.object_dir, 'training', 'front_view', '*.npy'))
            self.f_front.sort()
            self.f_label = glob.glob(os.path.join(self.object_dir, 'training', 'label_2', '*.txt'))
            self.f_label.sort()

        self.data_tag =  [name.split('/')[-1].split('.')[-2] for name in self.f_label]
        assert(len(self.f_rgb) == len(self.f_lidar) == len(self.f_label) == len(self.data_tag))
        self.dataset_size = len(self.f_rgb)
        self.alreay_extract_data = 0
        self.cur_frame_info = ''

        print("Dataset total length: {}".format(len(self.f_rgb)))
        if self.require_shuffle:
            self.shuffle_dataset()

        self.queue_size = queue_size
        self.require_shuffle = require_shuffle
        self.preprocess = data.Preprocess()
        self.dataset_queue = Queue()  # must use the queue provided by multiprocessing module(only this can be shared)


        self.load_index = 0
        if self.use_multi_process_num == 0:
            self.loader_worker = [threading.Thread(target=self.loader_worker_main)]
        else:
            self.loader_worker = [Process(target=self.loader_worker_main) for i in range(self.use_multi_process_num)]
        self.work_exit = Value('i', 0)
        [i.start() for i in self.loader_worker]

        # This operation is not thread-safe
        #import pycuda.autoinit # must do this after fork child
        try:
            # TODO: this must failed!
            tmp = self.load_specified()
            self.top_shape = tmp[3].shape
            self.front_shape = tmp[4].shape
            self.rgb_shape = tmp[1].shape
        except:
            # FIXME
            print('failed')
            self.top_shape = (800, 600, 27)
            self.front_shape = (cfg.FRONT_WIDTH, cfg.FRONT_HEIGHT, 3)
            self.rgb_shape = (cfg.IMAGE_HEIGHT, cfg.IMAGE_WIDTH, 3)
    
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.work_exit.value = True

    def __len__(self):
        return self.dataset_size

    def fill_queue(self, max_load_amount=0):
        
        # no need to do it here
        # def to_label(raw_labels):
        #     # input: lines of label file
        #     # return: [(str, (8, 3))]
        #     ret = []
        #     for line in raw_labels:
        #         data = line.split()
        #         obj_class = data[0]
        #         # camera coordinate
        #         h, w, l, x, y, z, ry = [float(i) for i in data[8:15]]
        #         # lidar coordinate
        #         # h, w, l, x, y, z, rz = h, l, w, z, -x, -y, -ry
        #         h, w, l, x, y, z, rz = h, w, l, box.camera_to_lidar_coords(x, y, z), -ry-math.pi/2
        #         ret.append((obj_class, box3d_compose((x, y, z), (h, w, l), (0, 0, rz))))
        #     return ret

        load_index = self.load_index
        self.load_index += max_load_amount
        for _ in range(max_load_amount):
            try:
                rgb = self.preprocess.rgb(cv2.imread(self.f_rgb[load_index]))
                raw_lidar = np.fromfile(self.f_lidar[load_index], dtype=np.float32).reshape((-1, 4))
                if self.use_precal_view:
                    try:
                        top_view = np.load(self.f_top[load_index])
                    except:
                        top_view = lidar_to_top_cuda(raw_lidar)
                    try:
                        front_view = np.load(self.f_front[load_index])
                    except:
                        front_view = lidar_to_front_cuda(raw_lidar)
                else: 
                    #print('before cuda')
                    top_view = lidar_to_top_cuda(raw_lidar)
                    # top_view[:, :, 26] = np.zeros_like(top_view[:, :, 0]) # 26 is density 
                    # top_view[:, :, 25] = np.zeros_like(top_view[:, :, 0]) # 25 is intensity
                # top_view = np.ones((400, 400, 10), dtype=np.float32)
                    front_view = lidar_to_front_cuda(raw_lidar)
                    #print('after cuda')
                # front_view = np.ones((cfg.FRONT_WIDTH, cfg.FRONT_HEIGHT, 3), dtype=np.float32)
                labels = [line for line in open(self.f_label[load_index], 'r').readlines()]
                tag = self.data_tag[load_index]

                self.dataset_queue.put_nowait((labels, rgb, raw_lidar, top_view, front_view, tag))
                load_index += 1
                # print("Fill {}, now size:{}".format(load_index, self.dataset_queue.qsize()))
            except:
                print('GG')
                if not self.is_testset:  # test set just end
                    self.load_index = 0
                    if self.require_shuffle:
                        self.shuffle_dataset()
                else:
                    self.work_exit.value = True

    def load(self):
        # output:
        # label: (B, N, ) (obj_class, (8,3))
        # rgb: (B, W, H)
        # raw lidar: (B, N, 4)
        # ...
        try: 
            label, rgb, raw_lidar, top_view, front_view, tag = [], [], [], [], [], []
            for _ in range(self.batch_size):
                #print("Queue size when load: {}, already extract:{}".format(self.dataset_queue.qsize(), self.alreay_extract_data))
                if self.is_testset and self.alreay_extract_data == self.dataset_size:
                    return None
                
                buff = self.dataset_queue.get()
                label.append(buff[0])
                rgb.append(buff[1])
                raw_lidar.append(buff[2])
                top_view.append(buff[3])
                front_view.append(buff[4])
                tag.append(buff[5])
                self.cur_frame_info = buff[5]

                self.alreay_extract_data += 1
            if self.is_testset:
                ret = (
                    np.array(tag),
                    np.array(rgb), 
                    np.array(raw_lidar),
                    np.array(top_view),
                    np.array(front_view)
                )
            else:
                ret = (
                    np.array(tag),
                    np.array(label), 
                    np.array(rgb),
                    np.array(raw_lidar),
                    np.array(top_view),
                    np.array(front_view)
                )
        except:
            print("Dataset empty!")
            ret = None
        return ret

    def load_specified(self, index=0):
        rgb = self.preprocess.rgb(cv2.imread(self.f_rgb[index]))
        raw_lidar = np.fromfile(self.f_lidar[index], dtype=np.float32).reshape((-1, 4))
        if self.use_precal_view:
            top_view = np.load(self.f_top[index])
            front_view = np.load(self.f_front[index])
        else: 
            top_view = lidar_to_top_cuda(raw_lidar)
        # top_view = np.ones((400, 400, 10), dtype=np.float32)
            front_view = lidar_to_front_cuda(raw_lidar)
        # front_view = np.ones((cfg.FRONT_WIDTH, cfg.FRONT_HEIGHT, 3), dtype=np.float32)
        labels = [line for line in open(self.f_label[index], 'r').readlines()]
        tag = self.data_tag[index]
        
        if self.is_testset:
            ret = (
                np.array([tag]),
                np.array([rgb]), 
                np.array([raw_lidar]),
                np.array([top_view]),
                np.array([front_view])
            )
        else:
            ret = (
                np.array([tag]),
                np.array([labels]), 
                np.array([rgb]),
                np.array([raw_lidar]),
                np.array([top_view]),
                np.array([front_view])
            )
        return ret


    def loader_worker_main(self):
        print('before start')
        print('start')
        import pycuda.autoinit 
        if self.require_shuffle:
            self.shuffle_dataset()
        while not self.work_exit.value:
            if self.dataset_queue.qsize() >= self.queue_size // 2:
                time.sleep(1)
            else:
                self.fill_queue(1)  # since we use multiprocessing, 1 is ok
                # print('fill one!')
        #print('exit!, current size:{}'.format(self.dataset_queue.qsize()))


    def get_shape(self):
        return self.top_shape, self.front_shape, self.rgb_shape

    def shuffle_dataset(self):
        # to prevent diff loader load same data
        index = shuffle([i for i in range(len(self.f_rgb))], random_state=random.randint(0, self.use_multi_process_num**5))
        self.f_label = [self.f_label[i] for i in index]
        self.f_rgb = [self.f_rgb[i] for i in index]
        self.f_lidar = [self.f_lidar[i] for i in index]
        self.f_top = [self.f_top[i] for i in index]
        self.f_front = [self.f_front[i] for i in index]
        self.data_tag = [self.data_tag[i] for i in index]

    def get_frame_info(self):
        return self.cur_frame_info

# for 3dop 2nd-stage testing, based on KittiLoading
# class Loading3DOP(object):
# 
#     def __init__(self, object_dir='.', proposals_dir='.', queue_size=20, require_shuffle=False, is_testset=True):
#         self.object_dir, self.proposals_dir = object_dir, proposals_dir
#         self.is_testset, self.require_shuffle = is_testset, require_shuffle
#         
#         self.f_proposal = glob.glob(os.path.join(self.proposals_dir, 'best/*_best.npy' if cfg.LOAD_BEST_PROPOSALS else 'all/*_all.npy'))
#         self.f_proposal.sort()
#         self.f_rgb = glob.glob(os.path.join(self.object_dir, 'training', 'image_2', '*.png'))
#         self.f_rgb.sort()
#         self.f_lidar = glob.glob(os.path.join(self.object_dir, 'training', 'velodyne', '*.bin'))
#         self.f_lidar.sort()
#         assert(len(self.f_proposal) == len(self.f_rgb) == len(self.f_lidar))
#         print(len(self.f_proposal))
#         if self.require_shuffle:
#             index = shuffle([i for i in range(len(self.f_proposal))])
#             self.f_proposal = [self.f_proposal[i] for i in index]
#             self.f_rgb = [self.f_rgb[i] for i in index]
#             self.f_lidar = [self.f_lidar[i] for i in index]
# 
#         self.queue_size = queue_size
#         self.require_shuffle = require_shuffle
#         self.preprocess = data.Preprocess()
# 
#         self.rgb_queue, self.front_view_queue, self.top_view_queue = Queue(), Queue(), Queue()
#         self.proposals_queue, self.proposal_scores_queue = Queue(), Queue()
# 
#         if not self.is_testset:
#             self.f_label = glob.glob(os.path.join(self.object_dir, 'training', 'label_2', '*.txt')).sort()
#             self.label_queue = Queue()
# 
#         self.load_index = 0
#         self.fill_queue(self.queue_size)
# 
#         # This operation is not thread-safe
#         try:
#             self.top_shape = self.top_view_queue.queue[0].shape
#             self.front_shape = self.front_view_queue.queue[0].shape 
#             self.rgb_shape = self.rgb_queue.queue[0].shape
#         except:
#             # FIXME
#             self.top_shape = (100, 100)
#             self.front_shape = (100, 100)
#             self.rgb_shape = (100, 100)
# 
#     def __enter__(self):
#         return self
# 
#     def __exit__(self, exc_type, exc_val, exc_tb):
#         pass
# 
#     def fill_queue(self, load_amount=0):
#         # no need to do it here
#         # def to_label(raw_labels):
#         #     # input: lines of label file
#         #     # return: [(str, (8, 3))]
#         #     ret = []
#         #     for line in raw_labels:
#         #         data = line.split()
#         #         obj_class = data[0]
#         #         # camera coordinate
#         #         h, w, l, x, y, z, ry = [float(i) for i in data[8:15]]
#         #         # lidar coordinate
#         #         # h, w, l, x, y, z, rz = h, l, w, z, -x, -y, -ry  # Such operations are not correct since there are transisition between lidar and camera coordinate
#         #         h, w, l, x, y, z, rz = h, w, l, box.camera_to_lidar_coords(x, y, z), -ry-math.pi/2
#         #         ret.append((obj_class, box3d_compose((x, y, z), (h, w, l), (0, 0, rz))))
#         #     return ret
# 
#         try:
#             for i in range(load_amount):
#                 # input: (N, 8)
#                 # print(self.load_index)
#                 proposals = np.load(self.f_proposal[self.load_index])
#                 while len(proposals) == 0: # seems that if feed in empty propsoal to model, it will stuck
#                     self.load_index += 1
#                     proposals = np.load(self.f_proposal[self.load_index])
# 
#                 self.proposals_queue.put(proposals[:, 0:7])
#                 self.proposal_scores_queue.put(proposals[:, 7])
#                 
#                 self.rgb_queue.put(self.preprocess.rgb(cv2.imread(self.f_rgb[self.load_index])))
#                 #self.rgb_queue.put(cv2.imread(self.f_rgb[self.load_index]))
# 
#                 raw_lidar = np.fromfile(self.f_lidar[self.load_index], dtype=np.float32).reshape((-1, 4))
#                 self.top_view_queue.put(lidar_to_top_cuda(raw_lidar))
#                 self.front_view_queue.put(lidar_to_front_cuda(raw_lidar))
# 
#                 if not self.is_testset:
#                     labels = [line for line in open(self.f_label[self.load_index], 'r').readlines()]
#                     self.label_queue.put(labels)
# 
#                 self.load_index += 1
#         except:
#             pass
# 
#     def load(self, batch_size=1):
#         try: 
#             if batch_size == 1: # currently only support batch_size == 1
#                 ret = (self.proposals_queue.get_nowait(), self.proposal_scores_queue.get_nowait(),
#                        self.top_view_queue.get_nowait(), self.front_view_queue.get_nowait(),
#                        self.rgb_queue.get_nowait())
#             else:
#                 ret = (np.array([self.proposals_queue.get_nowait() for i in range(batch_size)]),
#                       np.array([self.proposal_scores_queue.get_nowait() for i in range(batch_size)]),
#                       np.array([self.top_view_queue.get_nowait() for i in range(batch_size)]),
#                       np.array([self.front_view_queue.get_nowait() for i in range(batch_size)]),
#                       np.array([self.rgb_queue.get_nowait() for i in range(batch_size)]))
#             self.fill_queue(batch_size)
#         except:
#             ret = None 
#         return ret
# 
#     def get_shape(self):
#         return self.top_shape, self.front_shape, self.rgb_shape


class BatchLoading3:

    def __init__(self, bags={}, tags={}, queue_size=20, require_shuffle=False,
                 require_log=False, is_testset=False, batch_size=1, use_precal_view=False, use_multi_process_num=cpu_count()):
        self.is_testset = is_testset
        self.use_precal_view = use_precal_view
        self.shuffled = require_shuffle
        self.preprocess = data.Preprocess()
        self.raw_img = Image(tags)
        self.raw_tracklet = Tracklet(tags)
        self.raw_lidar = Lidar(tags)
        self.tags = self.raw_lidar.get_tags()
        self.batch_size = batch_size
        self.use_multi_process_num = use_multi_process_num
        print(len(self.raw_img.get_tags()), len(self.raw_lidar.get_tags()), len(self.raw_tracklet.get_tags()))
        assert(len(self.raw_img.get_tags()) == \
               len(self.raw_lidar.get_tags()) == \
               len(self.raw_tracklet.get_tags()))

        self.bags = bags
        # get all tags
        # self.tags = tags

        if self.shuffled:
            self.tags = shuffle(self.tags)

        self.tag_index = 0
        self.size = len(self.tags)

        self.require_log = require_log

        self.cache_size = queue_size
        self.loader_need_exit = Value('i', 0)

        self.prepr_data = Queue() # must use multiprocessing.Queue for sharing data
        if self.use_multi_process_num > 0:
            self.loader_processing = [Process(target=self.loader) for i in range(self.use_multi_process_num)]
        else:
            self.loader_processing = [threading.Thread(target=self.loader)]
            # self.preproc_data_queue = Queue()
            # self.buffer_blocks = [Array('h', 41246691) for i in range(queue_size)]
            # self.blocks_usage = Array('i', range(queue_size))
            # self.loader_processing = Process(target=self.loader)
        [i.start() for i in self.loader_processing]


    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.loader_need_exit.value=True
        if self.require_log: print('set loader_need_exit True')
        [i.join() for i in self.loader_processing]
        if self.require_log: print('exit loader_processing')

    def keep_gt_inside_range(self, train_gt_labels, train_gt_boxes3d):
        train_gt_labels = np.array(train_gt_labels, dtype=np.int32)
        train_gt_boxes3d = np.array(train_gt_boxes3d, dtype=np.float32)
        if train_gt_labels.shape[0] == 0:
            return False, None, None
        assert train_gt_labels.shape[0] == train_gt_boxes3d.shape[0]

        # get limited train_gt_boxes3d and train_gt_labels.
        keep = np.zeros((len(train_gt_labels)), dtype=bool)

        for i in range(len(train_gt_labels)):
            if box.box3d_in_top_view(train_gt_boxes3d[i]):
                keep[i] = 1

        # if all targets are out of range in selected top view, return True.
        if np.sum(keep) == 0:
            return False, None, None

        train_gt_labels = train_gt_labels[keep]
        train_gt_boxes3d = train_gt_boxes3d[keep]
        return True, train_gt_labels, train_gt_boxes3d

    def load_from_one_tag(self, one_frame_tag):
        if self.is_testset:
            obstacles = None
        else:
            obstacles = self.raw_tracklet.load(one_frame_tag)
        rgb = self.raw_img.load(one_frame_tag)
        lidar = self.raw_lidar.load(one_frame_tag)
        return obstacles, rgb, lidar


    def preprocess_one_frame(self, rgb, lidar, obstacles, tag):
        # attention: since we are training, there is no need to remove the other objects
        rgb = self.preprocess.rgb(rgb)
        if self.use_precal_view:
            top = np.load(os.path.join(cfg.RAW_DATA_SETS_DIR, 'top_view', tag + '.npy'))
            front = np.load(os.path.join(cfg.RAW_DATA_SETS_DIR, 'front_view', tag + '.npy'))
        else:
            top = lidar_to_top_cuda(lidar)
            front = lidar_to_front_cuda(lidar)
        if self.is_testset:
            return rgb, top, None, None
        boxes3d = [self.preprocess.bbox3d(obs) for obs in obstacles]
        labels = [self.preprocess.label(obs) for obs in obstacles]
        return rgb, top, boxes3d, labels, front

    def get_shape(self):
        train_rgbs, train_tops, train_fronts, train_gt_labels, train_gt_boxes3d, _ = self.load()
        top_shape = train_tops[0].shape
        front_shape = train_fronts[0].shape
        rgb_shape = train_rgbs[0].shape

        return top_shape, front_shape, rgb_shape

    def data_preprocessed(self):
        # only feed in frames with ground truth labels and bboxes during training, or the training nets will break.
        skip_frames = True
        batch_rgb, batch_top, batch_fronts, batch_labels, batch_boxes3d, batch_frame_tag = [], [], [], [], [], []
        for _ in range(self.batch_size):
            while skip_frames:
                # fronts = []
                frame_tag = self.tags[self.tag_index]
                obstacles, rgb, lidar = self.load_from_one_tag(frame_tag)
                rgb, top, boxes3d, labels, fronts = self.preprocess_one_frame(rgb, lidar, obstacles, frame_tag)
                if self.require_log and not self.is_testset:
                    draw_bbox_on_rgb(rgb, boxes3d, frame_tag)
                    draw_bbox_on_lidar_top(top, boxes3d, frame_tag)

                self.tag_index = self.tag_index + 1

                # reset self tag_index to 0 and shuffle tag list
                # nice job, so just training for more interation
                if self.tag_index >= self.size:
                    self.tag_index = 0
                    if self.shuffled:
                        self.tags = shuffle(self.tags, random_state=random.randint(0, self.use_multi_process_num**5))
                skip_frames = False

                # only feed in frames with ground truth labels and bboxes during training, or the training nets will break.

                if not self.is_testset:
                    is_gt_inside_range, batch_gt_labels_in_range, batch_gt_boxes3d_in_range = \
                        self.keep_gt_inside_range(labels, boxes3d)
                    labels = batch_gt_labels_in_range
                    boxes3d = batch_gt_boxes3d_in_range
                    # if no gt labels inside defined range, discard this training frame.
                    if not is_gt_inside_range:
                        skip_frames = True
            batch_rgb.append(rgb)
            batch_top.append(top)
            batch_fronts.append(fronts)
            batch_labels.append(labels)
            batch_boxes3d.append(boxes3d)
            batch_frame_tag.append(frame_tag)

        # return np.array([rgb]), np.array([top]), np.array([fronts]), np.array([labels]), \
        #        np.array([boxes3d]), frame_tag
        return np.array(batch_rgb), np.array(batch_top), np.array(batch_fronts), np.array(batch_labels),np.array(batch_boxes3d), np.array(batch_frame_tag)

    def find_empty_block(self):
        idx = -1
        for i in range(self.cache_size):
            if self.blocks_usage[i] == 1:
                continue
            else:
                idx = i
                break
        return idx


    def loader(self):
        # can only import at a process/thread
        import pycuda.autoinit 
        if True:
            self.tags = shuffle(self.tags, random_state=random.randint(0, self.use_multi_process_num**5))
            while self.loader_need_exit.value == 0:
                #if len(self.prepr_data) >=self.cache_size:
                if self.prepr_data.qsize() >= self.cache_size:
                    time.sleep(1)  # critic val 
                    #print('size {}'.format(self.prepr_data.qsize()))
                #    # print('sleep ')
                else:
                #self.prepr_data = [(self.data_preprocessed())]+self.prepr_data
                    self.prepr_data.put((self.data_preprocessed()))
                    #print('size {}'.format(self.prepr_data.qsize()))
                    #print('data_preprocessed')
        else:
            while self.loader_need_exit.value == 0:
                empty_idx = self.find_empty_block()
                if empty_idx == -1:
                    time.sleep(1)  # critic val for multi-process training 
                    # print('sleep ')
                else:
                    prepr_data = (self.data_preprocessed())
                    # print('data_preprocessed')
                    dumps = pickle.dumps(prepr_data)
                    length = len(dumps)
                    self.buffer_blocks[empty_idx][0:length] = dumps[0:length]

                    self.preproc_data_queue.put({
                        'index': empty_idx,
                        'length': length
                    })


        if self.require_log:print('loader exit')



    def load(self):
        if True:
            #while len(self.prepr_data)==0:
            data_ori = None 
            while data_ori == None:
                try:
                    data_ori = self.prepr_data.get(False)
                except:
                    pass 
            #while self.prepr_data.qsize() == 0:
                # print('data queue is empty!')
                #time.sleep(1)
            #data_ori = self.prepr_data.get()
        else:

            # print('self.preproc_data_queue.qsize() = ', self.preproc_data_queue.qsize())
            info = self.preproc_data_queue.get(block=True)
            length = info['length']
            block_index = info['index']
            dumps = self.buffer_blocks[block_index][0:length]

            #set flag
            self.blocks_usage[block_index] = 0

            # convert to bytes string
            dumps = array.array('B',dumps).tostring()
            data_ori = pickle.loads(dumps)

        return data_ori



    def get_frame_info(self):
        return self.tags[self.tag_index]


if __name__ == '__main__':
    # testing image testing, single frames
    # batch frame testing.
    dataset_dir = cfg.PREPROCESSED_DATA_SETS_DIR

    dates_to_drivers = {'1': ['11']}
    # dates_to_drivers = {'Round1Test': ['19_f2']}
    # load_indexs = None
    # batches = batch_loading(dataset_dir, dates_to_drivers, load_indexs, is_testset=True)
    # # get_shape is used for getting shape.
    # top_shape, front_shape, rgb_shape = batches.get_shape()
    # for i in range(1000):
    #     train_rgbs, train_tops, train_fronts, train_gt_labels, train_gt_boxes3d = batches.load(2, batch=True,
    #                                                                                            shuffled=False)

    # this code is for single testing.
    # load_indexs = ['00000', '00001', '00002', '00003']
    # batches = batch_loading(dataset_dir, dates_to_drivers, load_indexs, is_testset=True)
    #
    # for i in range(1000):
    #     train_rgbs, train_tops, train_fronts, train_gt_labels, train_gt_boxes3d, handle_id = batches.load(1, False)
    train_key_list = ['nissan_pulling_away',
                      'nissan_pulling_up_to_it',
                      'suburu_follows_capture',
                      'nissan_pulling_to_left',
                      'nissan_driving_past_it',
                      'nissan_pulling_to_right',
                      'suburu_driving_away',
                      'nissan_following_long',
                      'suburu_driving_parallel',
                      'suburu_driving_towards_it',
                      'suburu_pulling_to_left',
                      'suburu_not_visible',

                      'suburu_leading_front_left',
                      'ped_train',
                      'bmw_following_long',
                      'cmax_following_long',
                      'suburu_following_long',
                      'suburu_driving_past_it',
                      'nissan_brief',
                      'suburu_leading_at_distance']

    train_key_full_path_list = [os.path.join(cfg.RAW_DATA_SETS_DIR, key) for key in train_key_list]
    train_value_list = [os.listdir(value)[0] for value in train_key_full_path_list]

    train_n_val_dataset = [k + '/' + v for k, v in zip(train_key_list, train_value_list)]

    splitter = TrainingValDataSplitter(train_n_val_dataset)

    # bl = BatchLoading2(splitter.training_bags, splitter.training_tags)

    with BatchLoading2(splitter.training_bags, splitter.training_tags) as bl:
        time.sleep(5)
        for i in range(5):
            t0 = time.time()
            data = bl.load()
            print('use time =', time.time()-t0)
            print(data)
            time.sleep(3)

        print('Done')
