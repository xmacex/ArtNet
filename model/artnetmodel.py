"""Model for ArtNet."""

import sys
import logging
import pickle
import numpy as np
import cv2
sys.path.insert(0, '../Keras-FasterRCNN')
# from tensorflow import keras
import keras_frcnn.resnet as nn
from keras_frcnn import config
from keras_frcnn import roi_helpers
from keras import backend as K
from keras.layers import Input
from keras.models import Model


class ArtNetModel():
    """Model for ArtNet."""

    def __init__(self, config_file='model/config.pickle'):
        """Initialize the class."""
        self.logger = logging.getLogger()
        self.logger.setLevel(logging.DEBUG)

        with open(config_file, 'rb') as fd:
            self.C = pickle.load(fd)

        print(self.C)
        self.rpn, self.classifier, self.classifier_only = self.build_models()

    def build_models(self):
        """Build models."""
        self.class_mapping = {v: k for k, v in self.C.class_mapping.items()}
        img_input = Input(shape=(None, None, 3))
        roi_input = Input(shape=(self.C.num_rois, 4))
        feature_map_input = Input(shape=(None, None, 1024))

        # Define base network
        shared_layers = nn.nn_base(img_input, trainable=True)

        # Define RPN
        num_anchors = len(self.C.anchor_box_scales) * len(self.C.anchor_box_ratios)
        rpn_layers = nn.rpn(shared_layers, num_anchors)

        # The classifier
        classifier = nn.classifier(
            feature_map_input, roi_input,
            self.C.num_rois, nb_classes=len(self.class_mapping), trainable=True)

        # Define the models
        model_rpn = Model(img_input, rpn_layers)
        model_classifier_only = Model([feature_map_input, roi_input], classifier)
        model_classifier = Model([feature_map_input, roi_input], classifier)

        # Load weights
        model_rpn.load_weights(self.C.model_path, by_name=True)
        model_classifier.load_weights(self.C.model_path, by_name=True)

        # Compile the models
        model_rpn.compile(optimizer='sgd', loss='mse')
        model_classifier.compile(optimizer='sgd', loss='mse')

        return (model_rpn, model_classifier, model_classifier_only)

    def predict(self, data):
        """Predict on data received."""
        img = self.construct_image(data)
        X, ratio = self.format_img(img, self.C)

        if K.common.image_dim_ordering() == 'tf':
            X = np.transpose(X, (0, 2, 3, 1))

        # Get the feature maps and output from RPN
        [Y1, Y2, F] = self.rpn.predict(X)

        R = roi_helpers.rpn_to_roi(Y1, Y2,
                                   self.C, K.common.image_dim_ordering(),
                                   overlap_thresh=0.7)

        # convert from (x1, y1, x2, y2) to (x, y, w, h)
        R[:, 2] -= R[:, 0]
        R[:, 3] -= R[:, 1]

        bbox_threshold = 0.8
        bboxes = {}
        probs = {}
        #-------------------------------
        for jk in range(R.shape[0]//self.C.num_rois + 1):
            ROIs = np.expand_dims(R[self.C.num_rois*jk:self.C.num_rois*(jk+1), :], axis=0)
            if ROIs.shape[1] == 0:
                break

            if jk == R.shape[0]//self.C.num_rois:
                #pad R
                curr_shape = ROIs.shape
                target_shape = (curr_shape[0], self.C.num_rois, curr_shape[2])
                ROIs_padded = np.zeros(target_shape).astype(ROIs.dtype)
                ROIs_padded[:, :curr_shape[1], :] = ROIs
                ROIs_padded[0, curr_shape[1]:, :] = ROIs[0, 0, :]
                ROIs = ROIs_padded

            [P_cls, P_regr] = self.classifier_only.predict([F, ROIs])

            for ii in range(P_cls.shape[1]):

                # FIXME: What is going on in this conditional? It returns True and thus skips to next iteration
                # if np.max(P_cls[0, ii, :]) < bbox_threshold or np.argmax(P_cls[0, ii, :]) == (P_cls.shape[2] - 1):
                #    continue

                cls_name = self.class_mapping[np.argmax(P_cls[0, ii, :])]

                if cls_name not in bboxes:
                    bboxes[cls_name] = []
                    probs[cls_name] = []

                (x, y, w, h) = ROIs[0, ii, :]

                cls_num = np.argmax(P_cls[0, ii, :])
                try:
                    (tx, ty, tw, th) = P_regr[0, ii, 4*cls_num:4*(cls_num+1)]
                    tx /= self.C.classifier_regr_std[0]
                    ty /= self.C.classifier_regr_std[1]
                    tw /= self.C.classifier_regr_std[2]
                    th /= self.C.classifier_regr_std[3]
                    x, y, w, h = roi_helpers.apply_regr(x, y, w, h, tx, ty, tw, th)
                except:
                    pass
                bboxes[cls_name].append([self.C.rpn_stride*x, self.C.rpn_stride*y, self.C.rpn_stride*(x+w), self.C.rpn_stride*(y+h)])
                probs[cls_name].append(np.max(P_cls[0, ii, :]))
        #-------------------------------


        # return Y1, Y2, F
        # return R
        # raise # enter Flask's interactive debugger thing
        return bboxes, probs, img.shape[0], img.shape[1]
    def construct_image(self, data):
        """Construct an image from received data."""
        img = cv2.imdecode(np.asarray(bytearray(data)), cv2.IMREAD_UNCHANGED)
        self.logger.debug('Read image of shape %s', img.shape)

        return img






    

    # The below is just copied from test_frcnn.py, and modified for this class

    def format_img_size(self, img, C):
        """Formats the image size based on configuration C."""
        img_min_side = float(self.C.im_size)
        (height, width, _) = img.shape

        if width <= height:
            ratio = img_min_side/width
            new_height = int(ratio * height)
            new_width = int(img_min_side)
        else:
            ratio = img_min_side/height
            new_width = int(ratio * width)
            new_height = int(img_min_side)

        img = cv2.resize(img, (new_width, new_height), interpolation=cv2.INTER_CUBIC)
        return img, ratio

    def format_img_channels(self, img, C):
        """Formats the image channels based on configuration C."""
        img = img[:, :, (2, 1, 0)]
        img = img.astype(np.float32)
        img[:, :, 0] -= self.C.img_channel_mean[0]
        img[:, :, 1] -= self.C.img_channel_mean[1]
        img[:, :, 2] -= self.C.img_channel_mean[2]
        img /= self.C.img_scaling_factor
        img = np.transpose(img, (2, 0, 1))
        img = np.expand_dims(img, axis=0)

        return img

    def format_img(self, img, C):
        """Formats an image for model prediction based on configuration C."""
        img, ratio = self.format_img_size(img, C)
        img = self.format_img_channels(img, C)

        return img, ratio
