import tensorflow as tf
import numpy as np
import tensorflow.contrib.slim as slim
from tensorflow.python import pywrap_tensorflow

from lib.utils.config import cfg
from lib.network.inception_base import inception_base
from lib.network.vgg_base import vgg_base
from lib.rpn_layer.generate_anchors import generate_anchors
from lib.rpn_layer.anchor_target_layer_tf import anchor_target_layer
from lib.rpn_layer.proposal_layer_tf import proposal_layer

class CTPN(object):

    def __init__(self, is_train=False):
        self.img_input = tf.placeholder(tf.float32, shape=[None, None, None, 3], name="img_input")
        self.im_info = tf.placeholder(tf.float32, shape=[None, 3], name="im_info")

    def inference(self):

        proposal_predicted, proposal_cls_score, proposal_cls_prob = self.__ctpn_base()
        rpn_rois, rpn_targets = self.__proposal_layer(proposal_cls_prob, proposal_predicted)

        return rpn_rois

    def build_loss(self):

        self.gt_boxes = tf.placeholder(tf.float32, shape=[None, 5], name='gt_boxes')

        proposal_predicted, proposal_cls_score, proposal_cls_prob = self.__ctpn_base()
        rpn_labels, \
        rpn_bbox_targets, \
        rpn_bbox_inside_weights, \
        rpn_bbox_outside_weights = self.__anchor_layer(proposal_cls_score)

        # classification loss
        rpn_cls_score = tf.reshape(proposal_cls_prob, [-1, 2])  # shape (HxWxA, 2)
        rpn_label = tf.reshape(rpn_labels, [-1])  # shape (HxWxA)
        # ignore_label(-1)
        fg_keep = tf.equal(rpn_label, 1)
        rpn_keep = tf.where(tf.not_equal(rpn_label, -1))
        rpn_cls_score = tf.gather(rpn_cls_score, rpn_keep)  # shape (N, 2)
        rpn_label = tf.gather(rpn_label, rpn_keep)
        rpn_cross_entropy_n = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=rpn_label, logits=rpn_cls_score)

        # box loss TODO:回归2个值
        rpn_bbox_pred = proposal_predicted  # shape (1, H, W, Ax4)
        rpn_bbox_pred = tf.gather(tf.reshape(rpn_bbox_pred, [-1, 4]), rpn_keep)  # shape (N, 4)
        rpn_bbox_targets = tf.gather(tf.reshape(rpn_bbox_targets, [-1, 4]), rpn_keep)
        rpn_bbox_inside_weights = tf.gather(tf.reshape(rpn_bbox_inside_weights, [-1, 4]), rpn_keep)
        rpn_bbox_outside_weights = tf.gather(tf.reshape(rpn_bbox_outside_weights, [-1, 4]), rpn_keep)

        rpn_loss_box_n = tf.reduce_sum(rpn_bbox_outside_weights * self.__smooth_l1_dist(
            rpn_bbox_inside_weights * (rpn_bbox_pred - rpn_bbox_targets)), reduction_indices=[1])

        rpn_loss_box = tf.reduce_sum(rpn_loss_box_n) / (tf.reduce_sum(tf.cast(fg_keep, tf.float32)) + 1)
        rpn_cross_entropy = tf.reduce_mean(rpn_cross_entropy_n)

        model_loss = rpn_cross_entropy + rpn_loss_box

        regularization_losses = tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)
        total_loss = tf.add_n(regularization_losses) + model_loss

        return total_loss, model_loss, rpn_cross_entropy, rpn_loss_box


    def __ctpn_base(self):
        """
        特征提取层 feature extract layer
        :return: proposal_predicted : shape = [1, h, w, A*2]
                 proposal_cls_score: shape = [1, h, w, A*cfg["CLASSES_NUM"]]
                 proposal_cls_prob: shape = [1, h, w, A*cfg["CLASSES_NUM"]]
        """
        stddev = 0.01
        weight_decay = cfg["TRAIN"]["WEIGHT_DECAY"]

        with tf.variable_scope("CTPN_Network"):
            with slim.arg_scope([slim.conv2d, slim.fully_connected],
                                weights_initializer=tf.truncated_normal_initializer(0.0, stddev=stddev),
                                weights_regularizer=slim.l2_regularizer(weight_decay),
                                activation_fn=tf.nn.relu):
                inputs_img_tensor = tf.placeholder(tf.float32, shape=[None, None, None, 3])

                if cfg["BACKBONE"] == "InceptionNet":
                    features = inception_base(inputs_img_tensor)
                elif cfg["BACKBONE"] == "VggNet":
                    features = vgg_base(inputs_img_tensor)
                else:
                    assert 0, "error: backbone {} is not support!".format(cfg["BACKBONE"])

                features = slim.conv2d(features, 512, [3, 3], scope='rpn_conv_3x3')
                features_channel = tf.shape(features)[-1]
                features = self.__bilstm(features, features_channel, 128, features_channel)

                # proposal_predicted shape = [1, h, w, A*2] TODO:回归2个值
                proposal_predicted = slim.conv2d(features, len(cfg["ANCHOR_HEIGHT"]) * 4, [1, 1], scope='proposal_conv_1x1')
                # proposal_cls_score shape = [1, h, w, A*cfg["CLASSES_NUM"]]
                proposal_cls_score = slim.conv2d(features, len(cfg["ANCHOR_HEIGHT"]) * cfg["CLASSES_NUM"], [1, 1], scope='cls_conv_1x1')
                proposal_cls_score_shape = tf.shape(proposal_cls_score)
                # proposal_cls_score_reshape shape = [h*w*A, cfg["CLASSES_NUM"]]
                proposal_cls_score_reshape = tf.reshape(proposal_cls_score, [-1, cfg["CLASSES_NUM"]])
                # proposal_cls_prob shape = [1, h, w, A*cfg["CLASSES_NUM"]]
                proposal_cls_prob = tf.reshape(tf.nn.softmax(proposal_cls_score_reshape), proposal_cls_score_shape)

        return proposal_predicted, proposal_cls_score, proposal_cls_prob

    def __proposal_layer(self, proposal_cls_prob, proposal_predicted):
        """
        回归proposal框
        :param proposal_cls_prob: shape = [1, h, w, Axclass_num]
        :param proposal_predicted: shape = [1, h, w, Ax2] TODO:回归2个值
        :return rpn_rois : shape = [1 x H x W x A, 5]
                rpn_targets : shape = [1 x H x W x A, 2]
        """
        with tf.variable_scope("proposal_layer"):
            blob, bbox_delta = tf.py_func(proposal_layer,
                                          [proposal_cls_prob, proposal_predicted, self.im_info, "TEST", [cfg["ANCHOR_WIDTH"], ], [cfg["ANCHOR_WIDTH"]]],
                                          [tf.float32, tf.float32])

            rpn_rois = tf.reshape(blob, [-1, 5], name='rpn_rois')
            rpn_targets = tf.convert_to_tensor(bbox_delta, name='rpn_targets')
            return rpn_rois, rpn_targets

    def __anchor_layer(self, proposal_cls_score):
        with tf.variable_scope("anchor_layer"):
            # 'rpn_cls_score', 'gt_boxes', 'gt_ishard', 'dontcare_areas', 'im_info'
            rpn_labels, rpn_bbox_targets, rpn_bbox_inside_weights, rpn_bbox_outside_weights = \
                tf.py_func(anchor_target_layer,
                           [proposal_cls_score, self.gt_boxes, self.im_info, [cfg["ANCHOR_WIDTH"], ], [cfg["ANCHOR_WIDTH"]]],
                           [tf.float32, tf.float32, tf.float32, tf.float32])

            rpn_labels = tf.convert_to_tensor(tf.cast(rpn_labels, tf.int32),
                                              name='rpn_labels')  # shape is (1 x H x W x A, 2)
            rpn_bbox_targets = tf.convert_to_tensor(rpn_bbox_targets,
                                                    name='rpn_bbox_targets')  # shape is (1 x H x W x A, 4)
            rpn_bbox_inside_weights = tf.convert_to_tensor(rpn_bbox_inside_weights,
                                                           name='rpn_bbox_inside_weights')  # shape is (1 x H x W x A, 4)
            rpn_bbox_outside_weights = tf.convert_to_tensor(rpn_bbox_outside_weights,
                                                            name='rpn_bbox_outside_weights')  # shape is (1 x H x W x A, 4)

            return rpn_labels, rpn_bbox_targets, rpn_bbox_inside_weights, rpn_bbox_outside_weights

    def __bilstm(self, input, d_i, d_h, d_o, name="Bilstm"):
        """
        双向rnn
        :param input:
        :param d_i: 512 每个timestep 携带信息
        :param d_h: 128 一层rnn
        :param d_o: 512 最后rrn层输出
        :param name:
        :param trainable:
        :return:
        """
        with tf.variable_scope(name):
            shape = tf.shape(input)
            N, H, W, C = shape[0], shape[1], shape[2], shape[3]
            img = tf.reshape(input, [N * H, W, C])
            # print('dididididi',d_i)
            img.set_shape([None, None, d_i])

            lstm_fw_cell = tf.contrib.rnn.LSTMCell(d_h, state_is_tuple=True)
            lstm_bw_cell = tf.contrib.rnn.LSTMCell(d_h, state_is_tuple=True)

            lstm_out, last_state = tf.nn.bidirectional_dynamic_rnn(lstm_fw_cell,lstm_bw_cell, img, dtype=tf.float32)
            lstm_out = tf.concat(lstm_out, axis=-1)

            lstm_out = tf.reshape(lstm_out, [N * H * W, 2*d_h])

            outputs = slim.fully_connected(lstm_out, d_o)
            outputs = tf.reshape(outputs, [N, H, W, d_o])

            return outputs

    def __smooth_l1_dist(self, deltas, sigma2=9.0, name='smooth_l1_dist'):
        with tf.name_scope(name=name):
            deltas_abs = tf.abs(deltas)
            smoothL1_sign = tf.cast(tf.less(deltas_abs, 1.0/sigma2), tf.float32)
            return tf.square(deltas) * 0.5 * sigma2 * smoothL1_sign + \
                        (deltas_abs - 0.5 / sigma2) * tf.abs(smoothL1_sign - 1)

if __name__ == "__main__":
    pass
    # pretrain_model_path = './models/pretrain_model/inception_v4.ckpt'
    # reader = pywrap_tensorflow.NewCheckpointReader(pretrain_model_path)
    # keys = reader.get_variable_to_shape_map().keys()
    # print(keys)


