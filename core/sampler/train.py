# -*- coding: utf-8 -*-
#
# @File:   train.py
# @Author: Haozhe Xie
# @Date:   2023-04-10 10:46:37
# @Last Modified by: Haozhe Xie
# @Last Modified at: 2023-04-11 10:31:05
# @Email:  root@haozhexie.com

import logging
import math
import os
import torch
import shutil

import core.sampler.test
import models.vqgan
import models.sampler
import utils.average_meter
import utils.datasets
import utils.helpers
import utils.summary_writer

from time import time


def train(cfg):
    torch.backends.cudnn.benchmark = True
    # Set up networks
    local_rank = 0
    vqae = models.vqgan.VQAutoEncoder(cfg)
    sampler = models.sampler.AbsorbingDiffusionSampler(cfg)
    if torch.cuda.is_available():
        local_rank = torch.distributed.get_rank()
        logging.info("Start running the DDP on rank %d." % local_rank)
        device_id = local_rank % torch.cuda.device_count()
        vqae = torch.nn.parallel.DistributedDataParallel(
            vqae.to(device_id), device_ids=[device_id]
        )
        sampler = torch.nn.parallel.DistributedDataParallel(
            sampler.to(device_id), device_ids=[device_id]
        )
    else:
        vqae.device = torch.device("cpu")
        sampler.device = torch.device("cpu")

    # Load checkpoints (the ckpt of VQAE MUST provided)
    if "CKPT" not in cfg.CONST:
        raise Exception("The checkpoint of VQAE must be provided.")

    init_epoch = 0
    checkpoint = torch.load(cfg.CONST.CKPT)
    vqae.load_state_dict(checkpoint["vqae"])
    if "sampler" in checkpoint:
        logging.info("Recovering from %s ..." % (cfg.CONST.CKPT))
        sampler.load_state_dict(checkpoint["sampler"])
        init_epoch = checkpoint["init_epoch"]
        logging.info("Recover completed. Current epoch = #%d" % (init_epoch,))

    # Set up data loader
    train_dataset = utils.datasets.get_dataset(cfg, cfg.TRAIN.SAMPLER.DATASET, "train")
    train_sampler = None
    if torch.cuda.is_available():
        train_sampler = torch.utils.data.distributed.DistributedSampler(
            train_dataset, rank=local_rank, shuffle=True, drop_last=True
        )

    train_data_loader = torch.utils.data.DataLoader(
        dataset=train_dataset,
        batch_size=cfg.TRAIN.SAMPLER.BATCH_SIZE,
        num_workers=cfg.CONST.N_WORKERS,
        collate_fn=utils.datasets.collate_fn,
        pin_memory=False,
        sampler=train_sampler,
    )

    # Set up optimizers
    optimizer = torch.optim.Adam(
        filter(lambda p: p.requires_grad, sampler.parameters()),
        lr=cfg.TRAIN.SAMPLER.LR,
        weight_decay=cfg.TRAIN.SAMPLER.WEIGHT_DECAY,
        betas=cfg.TRAIN.SAMPLER.BETAS,
    )

    # Set up loss functions
    ce_loss = torch.nn.CrossEntropyLoss(ignore_index=-1, reduction="none")

    # Set up folders for logs, snapshot and checkpoints
    output_dir = os.path.join(cfg.DIR.OUTPUT, "%s", cfg.CONST.EXP_NAME)
    cfg.DIR.CHECKPOINTS = output_dir % "checkpoints"
    cfg.DIR.LOGS = output_dir % "logs"
    os.makedirs(cfg.DIR.CHECKPOINTS, exist_ok=True)

    # Summary writer
    if local_rank == 0:
        tb_writer = utils.summary_writer.SummaryWriter(cfg)

    # Training/Testing the network
    n_batches = len(train_data_loader)
    for epoch_idx in range(init_epoch + 1, cfg.TRAIN.SAMPLER.N_EPOCHS + 1):
        epoch_start_time = time()
        batch_time = utils.average_meter.AverageMeter()
        data_time = utils.average_meter.AverageMeter()
        losses = utils.average_meter.AverageMeter(["CodeIndexLoss", "Elbo", "RwElbo"])
        # Randomize the DistributedSampler
        if train_sampler:
            train_sampler.set_epoch(epoch_idx)

        batch_end_time = time()
        for batch_idx, data in enumerate(train_data_loader):
            n_itr = (epoch_idx - 1) * n_batches + batch_idx
            data_time.update(time() - batch_end_time)
            # TODO: Warm up

            input = utils.helpers.var_or_cuda(data["input"], vqae.device)
            with torch.no_grad():
                _, _, info = vqae.module.encode(input)

            x_0 = info["min_encoding_indices"].reshape(cfg.TRAIN.SAMPLER.BATCH_SIZE, -1)
            t, pt, x_0_hat_logits, x_0_ignore = sampler(x_0)
            code_index_loss = ce_loss(x_0_hat_logits, x_0_ignore).sum(1)
            elbo = code_index_loss / t / pt / (math.log(2) * x_0.size(1))
            rw_elbo = (
                (1 - (t / cfg.NETWORK.SAMPLER.TOTAL_STEPS))
                * code_index_loss
                / (math.log(2) * x_0.size(1))
            ).mean()
            losses.update(
                [code_index_loss.mean().item(), elbo.mean().item(), rw_elbo.item()]
            )
            sampler.zero_grad()
            rw_elbo.backward()
            optimizer.step()

            batch_time.update(time() - batch_end_time)
            batch_end_time = time()
            if local_rank == 0:
                tb_writer.add_scalars(
                    {
                        "Loss/Batch/CodeIndex": losses.val(0),
                        "Loss/Batch/ELBO": losses.val(1),
                        "Loss/Batch/RwELBO": losses.val(2),
                    },
                    n_itr,
                )
                logging.info(
                    "[Epoch %d/%d][Batch %d/%d] BatchTime = %.3f (s) DataTime = %.3f (s) Losses = %s"
                    % (
                        epoch_idx,
                        cfg.TRAIN.SAMPLER.N_EPOCHS,
                        batch_idx + 1,
                        n_batches,
                        batch_time.val(),
                        data_time.val(),
                        ["%.4f" % l for l in losses.val()],
                    )
                )
        # TODO: Enable it later
        # lr_scheduler.step()
        epoch_end_time = time()
        if local_rank == 0:
            tb_writer.add_scalars(
                {
                    "Loss/Epoch/CodeIndex/Train": losses.avg(0),
                    "Loss/Epoch/ELBO/Train": losses.avg(1),
                    "Loss/Epoch/RwELBO/Train": losses.vavgal(2),
                },
                epoch_idx,
            )
            logging.info(
                "[Epoch %d/%d] EpochTime = %.3f (s) Losses = %s"
                % (
                    epoch_idx,
                    cfg.TRAIN.VQGAN.N_EPOCHS,
                    epoch_end_time - epoch_start_time,
                    ["%.4f" % l for l in losses.avg()],
                )
            )

        # Evaluate the current model
        key_frames = core.sampler.test(cfg, sampler)
        if local_rank == 0:
            tb_writer.add_images(key_frames, epoch_idx)
            # Save ckeckpoints
            logging.info("Saved checkpoint to ckpt-last.pth ...")
            torch.save(
                {
                    "cfg": cfg,
                    "epoch_index": epoch_idx,
                    "vqae": vqae.state_dict(),
                    "sampler": sampler.state_dict(),
                },
                os.path.join(cfg.DIR.CHECKPOINTS, "ckpt-last.pth"),
            )
            if epoch_idx % cfg.TRAIN.SAMPLER.CKPT_SAVE_FREQ == 0:
                shutil.copy(
                    os.path.join(cfg.DIR.CHECKPOINTS, "ckpt-last.pth"),
                    os.path.join(
                        cfg.DIR.CHECKPOINTS, "ckpt-epoch-%03d.pth" % epoch_idx
                    ),
                )

    if local_rank == 0:
        tb_writer.close()
