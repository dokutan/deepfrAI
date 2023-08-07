from __future__ import absolute_import

import os
import logging
from collections import OrderedDict
import torch
import torch.nn as nn

import models.networks as networks
from .base_model import BaseModel
from . import losses
from dataops.colors import ycbcr_to_rgb

import torch.nn.functional as F
from dataops.debug import tmp_vis, tmp_vis_flow, describe_numpy, describe_tensor

logger = logging.getLogger('base')


class VSRModel(BaseModel):
    def __init__(self, opt):
        super(VSRModel, self).__init__(opt)
        train_opt = opt['train']
        self.scale = opt.get('scale', 4)
        self.tensor_shape = opt.get('tensor_shape', 'TCHW')

        # specify the models you want to load/save to the disk.
        # The training/test scripts will call <BaseModel.save_networks>
        # and <BaseModel.load_networks>
        # for training and testing, a generator 'G' is needed
        self.model_names = ['G']

        # define networks and load pretrained models
        self.netG = networks.define_G(opt).to(self.device)  # G
        if self.is_train:
            self.netG.train()
            opt_G_nets = [self.netG]
            opt_D_nets = []
            if train_opt['gan_weight']:
                self.model_names.append('D')  # add discriminator to the network list
                self.netD = networks.define_D(opt).to(self.device)  # D
                self.netD.train()
                opt_D_nets.append(self.netD)
        self.load()  # load G and D if needed

        # define losses, optimizer, scheduler and other components
        if self.is_train:
            # setup network cap
            # define if the generator will have a final
            # capping mechanism in the output
            self.outm = train_opt.get('finalcap', None)

            # setup frequency separation
            self.setup_fs()

            # initialize losses
            # generator losses:
            self.generatorlosses = losses.GeneratorLoss(opt, self.device)

            # TODO: show the configured losses names in logger
            # print(self.generatorlosses.loss_list)

            # discriminator loss:
            self.setup_gan()

            # Optical Flow Reconstruction loss:
            ofr_type = train_opt.get('ofr_type', None)
            ofr_weight = train_opt.get('ofr_weight', [0.1, 0.2, 0.1, 0.01])
            if ofr_type and ofr_weight:
                self.ofr_weight = ofr_weight[3] #lambda 4
                self.ofr_wl1 = ofr_weight[0] #lambda 1
                self.ofr_wl2 = ofr_weight[1] #lambda 2
                ofr_wl3 = ofr_weight[2] #lambda 3
                if ofr_type == 'ofr':
                    from models.modules.loss import OFR_loss
                    #TODO: make the regularization weight an option. lambda3 = 0.1
                    self.cri_ofr = OFR_loss(reg_weight=ofr_wl3).to(self.device)
            else:
                self.cri_ofr = False
 
            # configure FreezeD
            if self.cri_gan:
                self.setup_freezeD()

            # prepare optimizers
            self.setup_optimizers(opt_G_nets, opt_D_nets, init_setup=True)

            # prepare schedulers
            self.setup_schedulers()

            # set gradients to zero
            self.optimizer_G.zero_grad()
            if self.cri_gan:
                self.optimizer_D.zero_grad()

            # init loss log
            self.log_dict = OrderedDict()

            # configure SWA
            self.setup_swa()

            # configure virtual batch
            self.setup_virtual_batch()

            # configure AMP
            self.setup_amp()

        # print network
        # TODO: pass verbose flag from config file
        self.print_network(verbose=False)

    def feed_data(self, data, need_HR=True):
        # data
        if len(data['LR'].size()) == 4:
            b, n_frames, h_lr, w_lr = data['LR'].size()
            LR = data['LR'].view(b, -1, 1, h_lr, w_lr)  # b, t, c, h, w
        elif len(data['LR'].size()) == 5:  # for networks that work with 3 channel images
            if self.tensor_shape == 'CTHW':
                _, _, n_frames, _, _ = data['LR'].size()  # b, c, t, h, w
            else:
                # TCHW
                _, n_frames, _, _, _ = data['LR'].size()  # b, t, c, h, w
            LR = data['LR']

        self.idx_center = (n_frames - 1) // 2
        self.n_frames = n_frames

        # LR images (LR_y_cube)
        self.var_L = LR.to(self.device)

        # bicubic upscaled LR and RGB center HR
        if isinstance(data['HR_center'], torch.Tensor):
            self.real_H_center = data['HR_center'].to(self.device)
        else:
            self.real_H_center = None
        if isinstance(data['LR_bicubic'], torch.Tensor): 
            self.var_LR_bic = data['LR_bicubic'].to(self.device)
        else:
            self.var_LR_bic = None

        if need_HR:  # train or val
            # HR images
            if len(data['HR'].size()) == 4:
                HR = data['HR'].view(b, -1, 1, h_lr * self.scale, w_lr * self.scale)  # b, t, c, h, w
            elif len(data['HR'].size()) == 5:  # for networks that work with 3 channel images
                HR = data['HR'] # b, t, c, h, w 
            self.real_H = HR.to(self.device)

            # discriminator references
            input_ref = data.get('ref', data['HR'])
            if len(input_ref.size()) == 4:
                input_ref = input_ref.view(b, -1, 1, h_lr * self.scale, w_lr * self.scale)  # b, t, c, h, w
                self.var_ref = input_ref.to(self.device)
            elif len(input_ref.size()) == 5:  # for networks that work with 3 channel images
                self.var_ref = input_ref.to(self.device)

    def feed_data_batch(self, data, need_HR=True):
        # TODO
        # LR
        self.var_L = data

    def optimize_parameters(self, step):
        """Calculate losses, gradients, and update network weights;
        called in every training iteration."""
        eff_step = step/self.accumulations

        # G
        # freeze discriminator while generator is trained to prevent BP
        if self.cri_gan:
            self.requires_grad(self.netD, flag=False, net_type='D')

        # Network forward, generate SR
        with self.cast():
            # inference
            self.fake_H = self.netG(self.var_L)
            if not isinstance(self.fake_H, torch.Tensor) and len(self.fake_H) == 4:
                flow_L1, flow_L2, flow_L3, self.fake_H = self.fake_H
        #/with self.cast():

        # TODO: TMP test to view samples of the optical flows
        # tmp_vis(self.real_H[:, self.idx_center, :, :, :], True)
        # print(flow_L1[0].shape)
        # tmp_vis(flow_L1[0][:, 0:1, :, :], to_np=True, rgb2bgr=False)
        # tmp_vis(flow_L2[0][:, 0:1, :, :], to_np=True, rgb2bgr=False)
        # tmp_vis(flow_L3[0][:, 0:1, :, :], to_np=True, rgb2bgr=False)
        # tmp_vis_flow(flow_L1[0])
        # tmp_vis_flow(flow_L2[0])
        # tmp_vis_flow(flow_L3[0])

        # calculate and log losses
        loss_results = []
        l_g_total = 0
        # training generator and discriminator
        # update generator (on its own if only training generator or alternatively if training GAN)
        if (self.cri_gan is not True) or (step % self.D_update_ratio == 0 and step > self.D_init_iters):
            with self.cast(): # Casts operations to mixed precision if enabled, else nullcontext
                # get the central frame for SR losses
                if isinstance(self.var_LR_bic, torch.Tensor) and isinstance(self.real_H_center, torch.Tensor):
                    # tmp_vis(ycbcr_to_rgb(self.var_LR_bic), True)
                    # print("fake_H:", self.fake_H.shape)
                    fake_H_cb = self.var_LR_bic[:, 1, :, :].to(self.device)
                    # print("fake_H_cb: ", fake_H_cb.shape)
                    fake_H_cr = self.var_LR_bic[:, 2, :, :].to(self.device)
                    # print("fake_H_cr: ", fake_H_cr.shape)
                    centralSR = ycbcr_to_rgb(torch.stack((self.fake_H.squeeze(1), fake_H_cb, fake_H_cr), -3))
                    # print("central rgb", centralSR.shape)
                    # tmp_vis(centralSR, True)
                    # centralHR = ycbcr_to_rgb(self.real_H_center) #Not needed, can send the rgb HR from dataloader
                    centralHR = self.real_H_center
                    # print(centralHR.shape)
                    # tmp_vis(centralHR)
                else:
                    # if self.var_L.shape[2] == 1:
                    centralSR = self.fake_H
                    centralHR = self.real_H[:, :, self.idx_center, :, :] if self.tensor_shape == 'CTHW' else self.real_H[:, self.idx_center, :, :, :]

                # tmp_vis(torch.cat((centralSR, centralHR), -1))

                # regular losses
                # loss_SR = criterion(self.fake_H, self.real_H[:, idx_center, :, :, :]) #torch.nn.MSELoss()
                loss_results, self.log_dict = self.generatorlosses(
                    centralSR, centralHR, self.log_dict, self.f_low)
                l_g_total += sum(loss_results)/self.accumulations

                # optical flow reconstruction loss
                # TODO: see if can be moved into loss file
                # TODO 2: test if AMP could affect the loss due to loss of precision
                if self.cri_ofr:  # OFR_loss()
                    l_g_ofr = 0
                    for i in range(self.n_frames):
                        if i != self.idx_center:
                            loss_L1 = self.cri_ofr(
                                F.avg_pool2d(self.var_L[:, i, :, :, :], kernel_size=2),
                                F.avg_pool2d(self.var_L[:, self.idx_center, :, :, :], kernel_size=2),
                                flow_L1[i])
                            loss_L2 = self.cri_ofr(
                                self.var_L[:, i, :, :, :],
                                self.var_L[:, self.idx_center, :, :, :], flow_L2[i])
                            loss_L3 = self.cri_ofr(
                                self.real_H[:, i, :, :, :],
                                self.real_H[:, self.idx_center, :, :, :], flow_L3[i])
                            # ofr weights option. lambda2 = 0.2, lambda1 = 0.1 in the paper
                            l_g_ofr += loss_L3 + self.ofr_wl2 * loss_L2 + self.ofr_wl1 * loss_L1

                    # ofr weight option. lambda4 = 0.01 in the paper
                    l_g_ofr = self.ofr_weight * l_g_ofr / (self.n_frames - 1)
                    self.log_dict['ofr'] = l_g_ofr.item()
                    l_g_total += l_g_ofr/self.accumulations

                if self.cri_gan:
                    # adversarial loss
                    l_g_gan = self.adversarial(
                        centralSR, centralHR, netD=self.netD,
                        stage='generator', fsfilter = self.f_high)  # (sr, hr)
                    self.log_dict['l_g_gan'] = l_g_gan.item()
                    l_g_total += l_g_gan/self.accumulations

            #/with self.cast():

            # high precision generator losses (can be affected by AMP half precision)
            if self.generatorlosses.precise_loss_list:
                loss_results, self.log_dict = self.generatorlosses(
                    centralSR, centralHR, self.log_dict, self.f_low,
                    precise=True)
                l_g_total += sum(loss_results)/self.accumulations

            # calculate G gradients
            self.calc_gradients(l_g_total)

            # step G optimizer
            self.optimizer_step(step, self.optimizer_G, "G")

        if self.cri_gan:
            # update discriminator
            # unfreeze discriminator
            for p in self.netD.parameters():
                p.requires_grad = True
            l_d_total = 0

            with self.cast():  # Casts operations to mixed precision if enabled, else nullcontext
                l_d_total, gan_logs = self.adversarial(
                    centralSR, centralHR, netD=self.netD,
                    stage='discriminator', fsfilter = self.f_high)  # (sr, hr)

                for g_log in gan_logs:
                    self.log_dict[g_log] = gan_logs[g_log]

                l_d_total /= self.accumulations
            # /with autocast():

            # calculate G gradients
            self.calc_gradients(l_d_total)

            # step D optimizer
            self.optimizer_step(step, self.optimizer_D, "D")

    def test(self):
        # TODO: test/val code
        self.netG.eval()
        with torch.no_grad():
            if self.is_train:
                self.fake_H = self.netG(self.var_L)
                if len(self.fake_H) == 4:
                    _, _, _, self.fake_H = self.fake_H
            else:
                # self.fake_H = self.netG(self.var_L, isTest=True)
                self.fake_H = self.netG(self.var_L)
                if len(self.fake_H) == 4:
                    _, _, _, self.fake_H = self.fake_H
        self.netG.train()

    def get_current_log(self):
        return self.log_dict

    def get_current_visuals(self, need_HR=True):
        # TODO: temporal considerations
        out_dict = OrderedDict()
        out_dict['LR'] = self.var_L.detach()[0].float().cpu()
        out_dict['SR'] = self.fake_H.detach()[0].float().cpu()
        if need_HR:
            out_dict['HR'] = self.real_H.detach()[0].float().cpu()
        return out_dict

    def get_current_visuals_batch(self, need_HR=True):
        # TODO: temporal considerations
        out_dict = OrderedDict()
        out_dict['LR'] = self.var_L.detach().float().cpu()
        out_dict['SR'] = self.fake_H.detach().float().cpu()
        if need_HR:
            out_dict['HR'] = self.real_H.detach().float().cpu()
        return out_dict
