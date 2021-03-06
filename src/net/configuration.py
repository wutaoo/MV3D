from easydict import*
import numpy as np
import simplejson as jason




CFG = EasyDict()

#train -----------------------------------------
CFG.TRAIN = EasyDict()

#all
CFG.TRAIN.IMS_PER_BATCH = 1  # Images to use per minibatch

#rpn
CFG.TRAIN.RPN_BATCHSIZE    = 256  # this is the amount of anchors for training RPN
CFG.TRAIN.RPN_FG_FRACTION  = 0.25
CFG.TRAIN.RPN_FG_THRESH_LO = 0.5 # cxz choice   # To decided which is FG(by overlap)
CFG.TRAIN.RPN_BG_THRESH_HI = 0.3 # cxz choice   # To decided which is BG(by overlap)

CFG.TRAIN.RPN_NMS_THRESHOLD = 0.5 # 0.7 can be override when using the nms generator, so deprecated(look at config.py instead)
CFG.TRAIN.RPN_NMS_MIN_SIZE  = 8   # to trunc some extreme small bbox(w.r.t. the real size of the image)
CFG.TRAIN.RPN_NMS_PRE_TOPN  = 1000 #100#6000 #12000 6000
CFG.TRAIN.RPN_NMS_POST_TOPN = 30 #30#512 #2000 1200  this is the amount of proposals output by the RPN(Anytime)


#rcnn
CFG.TRAIN.RCNN_BATCH_SIZE   = 128  #This is the amount of rois output to rcnn on training(somehow should be less than RPN_NMS_POST_TOPN)
CFG.TRAIN.RCNN_FG_FRACTION  = 0.25
CFG.TRAIN.RCNN_BG_THRESH_HI = 0.01 # 0.5  to decided which is negative sample togather with BG_THRESH_LO (by overlap)
CFG.TRAIN.RCNN_BG_THRESH_LO = 0    # 0.1
CFG.TRAIN.RCNN_FG_THRESH_LO = 0.5  # to decided which is positive sample(by overlap)
CFG.TRAIN.RCNN_box_NORMALIZE_STDS = (0.1, 0.1, 0.2, 0.2)


#test -----------------------------------------
CFG.TEST  = EasyDict()

CFG.TEST.RCNN_NMS_AFTER = 0.3
CFG.TEST.RCNN_box_NORMALIZE_STDS = CFG.TRAIN.RCNN_box_NORMALIZE_STDS
CFG.TEST.USE_box_VOTE = 1



def merge_a_into_b(a, b):
    """  Merge config dictionary a into config dictionary b, clobbering the
         options in b whenever they are also specified in a.
    """

    if type(a) is not EasyDict:
        return

    for k, v in a.iteritems():
        # a must specify keys that are in b
        if not b.has_key(k):
            raise KeyError('{} is not a valid config key'.format(k))

        # the types must match, too
        old_type = type(b[k])
        if old_type is not type(v):
            if isinstance(b[k], np.ndarray):
                v = np.array(v, dtype=b[k].dtype)
            else:
                raise ValueError(('Type mismatch ({} vs. {}) '
                                'for config key: {}').format(type(b[k]), type(v), k))

        # recursively merge dicts
        if type(v) is EasyDict:
            try:
                merge_a_into_b(a[k], b[k])
            except:
                print('Error under config key: {}'.format(k))
                raise
        else:
            b[k] = v


def read_cfg(file):
    """Load a config file and merge it into the default options."""

    with open(file, 'r') as f:
        cfg = EasyDict(jason.load(f))

    #merge into CFG from configuation.py
    merge_a_into_b(cfg, CFG)


def write_cfg(file):

    with open(file, 'w') as f:
        jason.dump(CFG, f, indent=4)


