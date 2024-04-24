# --------------------------------------------------------
# Copyright (c) 2023 Microsoft
# Licensed under The MIT License
# --------------------------------------------------------

import multiprocessing
import torch
import math
from torch.cuda.amp import GradScaler
from torch.distributed.elastic.multiprocessing import errors

from utils import logger
from options.opts import get_training_arguments
from utils.common_utils import device_setup, create_directories
from utils.ddp_utils import is_master, distributed_init
from affnet import get_model, EMA
from loss_fn import build_loss_fn
from optim import build_optimizer
from optim.scheduler import build_scheduler
from data import create_train_val_loader
from utils.checkpoint_utils import load_checkpoint, load_model_state
from engine import Trainer
from common import (
    DEFAULT_EPOCHS,
    DEFAULT_ITERATIONS,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_EPOCHS,
)
# from torchstat import stat
# from torch.profiler import profile, record_function, ProfilerActivity

try:
    import wandb
    has_wandb = True
except ImportError:
    has_wandb = False


@errors.record
def main(opts, **kwargs):
    if is_master(opts):
        print("init wandb")
        opts.log_wandb = opts.log_wandb and has_wandb
        if opts.log_wandb:
            wandb.init(project='affnet',
                       config=opts,
                       name=opts.experiment,
                       id=opts.experiment,
                       tags=[
                           'backbone',
                       ],
                       settings=wandb.Settings(start_method="fork"),
                       resume='allow',
                       )
    num_gpus = getattr(opts, "dev.num_gpus", 0)  # defaults are for CPU
    dev_id = getattr(opts, "dev.device_id", torch.device("cpu"))
    device = getattr(opts, "dev.device", torch.device("cpu"))
    is_distributed = getattr(opts, "ddp.use_distributed", False)

    is_master_node = is_master(opts)

    # set-up data loaders
    train_loader, val_loader, train_sampler = create_train_val_loader(opts)

    # compute max iterations based on max epochs
    # Useful in doing polynomial decay
    is_iteration_based = getattr(opts, "scheduler.is_iteration_based", False)
    if is_iteration_based:
        max_iter = getattr(opts, "scheduler.max_iterations", DEFAULT_ITERATIONS)
        if max_iter is None or max_iter <= 0:
            logger.log("Setting max. iterations to {}".format(DEFAULT_ITERATIONS))
            setattr(opts, "scheduler.max_iterations", DEFAULT_ITERATIONS)
            max_iter = DEFAULT_ITERATIONS
        setattr(opts, "scheduler.max_epochs", DEFAULT_MAX_EPOCHS)
        if is_master_node:
            logger.log("Max. iteration for training: {}".format(max_iter))
    else:
        max_epochs = getattr(opts, "scheduler.max_epochs", DEFAULT_EPOCHS)
        if max_epochs is None or max_epochs <= 0:
            logger.log("Setting max. epochs to {}".format(DEFAULT_EPOCHS))
            setattr(opts, "scheduler.max_epochs", DEFAULT_EPOCHS)
        setattr(opts, "scheduler.max_iterations", DEFAULT_MAX_ITERATIONS)
        max_epochs = getattr(opts, "scheduler.max_epochs", DEFAULT_EPOCHS)
        if is_master_node:
            logger.log("Max. epochs for training: {}".format(max_epochs))
    # set-up the model
    model = get_model(opts)

    # # add by huangzp
    # stat(model, (3, 224, 224))
    #
    # # torch profiler
    # with profile(activities=[ProfilerActivity.CPU], record_shapes=True) as prof:
    #     with record_function("model_inference"):
    #         model(input)

    # memory format
    memory_format = (
        torch.channels_last
        if getattr(opts, "common.channels_last", False)
        else torch.contiguous_format
    )

    if num_gpus == 0:
        logger.warning(
            "No GPUs are available, so training on CPU. Consider training on GPU for faster training"
        )
        model = model.to(device=device, memory_format=memory_format)
    elif num_gpus == 1:
        model = model.to(device=device, memory_format=memory_format)
    elif is_distributed:
        model = model.to(device=device, memory_format=memory_format)
        model = torch.nn.parallel.DistributedDataParallel(
            model,
            device_ids=[dev_id],
            output_device=dev_id,
            find_unused_parameters=getattr(opts, "ddp.find_unused_params", False),
        )
        if is_master_node:
            logger.log("Using DistributedDataParallel for training")
    else:
        model = model.to(memory_format=memory_format)
        model = torch.nn.DataParallel(model)
        model = model.to(device=device)
        if is_master_node:
            logger.log("Using DataParallel for training")

    # setup criteria
    criteria = build_loss_fn(opts)
    criteria = criteria.to(device=device)

    # create the optimizer
    optimizer = build_optimizer(model, opts=opts)

    # create the gradient scalar
    gradient_scalar = GradScaler(enabled=getattr(opts, "common.mixed_precision", False))

    # LR scheduler
    scheduler = build_scheduler(opts=opts)

    model_ema = None
    use_ema = getattr(opts, "ema.enable", False)

    if use_ema:
        ema_momentum = getattr(opts, "ema.momentum", 0.0001)
        model_ema = EMA(model=model, ema_momentum=ema_momentum, device=device)
        if is_master_node:
            logger.log("Using EMA")

    best_metric = (
        0.0 if getattr(opts, "stats.checkpoint_metric_max", False) else math.inf
    )

    start_epoch = 0
    start_iteration = 0
    resume_loc = getattr(opts, "common.resume", None)
    finetune_loc = getattr(opts, "common.finetune_imagenet1k", None)
    auto_resume = getattr(opts, "common.auto_resume", False)
    if resume_loc is not None or auto_resume:
        (
            model,
            optimizer,
            gradient_scalar,
            start_epoch,
            start_iteration,
            best_metric,
            model_ema,
        ) = load_checkpoint(
            opts=opts,
            model=model,
            optimizer=optimizer,
            model_ema=model_ema,
            gradient_scalar=gradient_scalar,
        )
    elif finetune_loc is not None:
        model, model_ema = load_model_state(opts=opts, model=model, model_ema=model_ema)
        if is_master_node:
            logger.log("Finetuning model from checkpoint {}".format(finetune_loc))

    training_engine = Trainer(
        opts=opts,
        model=model,
        validation_loader=val_loader,
        training_loader=train_loader,
        optimizer=optimizer,
        criterion=criteria,
        scheduler=scheduler,
        start_epoch=start_epoch,
        start_iteration=start_iteration,
        best_metric=best_metric,
        model_ema=model_ema,
        gradient_scalar=gradient_scalar,
    )

    training_engine.run(train_sampler=train_sampler)


def distributed_worker(i, main, opts, kwargs):
    setattr(opts, "dev.device_id", i)
    torch.cuda.set_device(i)
    setattr(opts, "dev.device", torch.device(f"cuda:{i}"))

    ddp_rank = getattr(opts, "ddp.rank", None)
    if ddp_rank is None:  # torch.multiprocessing.spawn
        ddp_rank = kwargs.get("start_rank", 0) + i
        setattr(opts, "ddp.rank", ddp_rank)

    node_rank = distributed_init(opts)
    setattr(opts, "ddp.rank", node_rank)
    main(opts, **kwargs)


def main_worker(**kwargs):
    opts = get_training_arguments()
    print(opts)
    # device set-up
    opts = device_setup(opts)

    node_rank = getattr(opts, "ddp.rank", 0)
    if node_rank < 0:
        logger.error("--rank should be >=0. Got {}".format(node_rank))

    is_master_node = is_master(opts)

    # create the directory for saving results
    save_dir = getattr(opts, "common.results_loc", "results")
    run_label = getattr(opts, "common.run_label", "run_1")
    exp_dir = "{}/{}".format(save_dir, run_label)
    setattr(opts, "common.exp_loc", exp_dir)
    create_directories(dir_path=exp_dir, is_master_node=is_master_node)

    num_gpus = getattr(opts, "dev.num_gpus", 1)
    world_size = getattr(opts, "ddp.world_size", -1)
    use_distributed = not getattr(opts, "ddp.disable", False)
    if num_gpus <= 1:
        use_distributed = False
    setattr(opts, "ddp.use_distributed", use_distributed)

    # No of data workers = no of CPUs (if not specified or -1)
    n_cpus = multiprocessing.cpu_count()
    dataset_workers = getattr(opts, "dataset.workers", -1)

    norm_name = getattr(opts, "model.normalization.name", "batch_norm")
    ddp_spawn = not getattr(opts, "ddp.no_spawn", False)
    if use_distributed and ddp_spawn and torch.cuda.is_available():
        # get device id
        dev_id = getattr(opts, "ddp.device_id", None)
        setattr(opts, "dev.device_id", dev_id)

        if world_size == -1:
            logger.log(
                "Setting --ddp.world-size the same as the number of available gpus"
            )
            world_size = num_gpus
            setattr(opts, "ddp.world_size", world_size)

        if dataset_workers == -1 or dataset_workers is None:
            setattr(opts, "dataset.workers", n_cpus // num_gpus)

        start_rank = getattr(opts, "ddp.rank", 0)
        setattr(opts, "ddp.rank", None)
        kwargs["start_rank"] = start_rank
        setattr(opts, "ddp.start_rank", start_rank)

        # add by huangzp: fix the distribution error of rr1
        def _find_free_port():
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Binding to port 0 will cause the OS to find an available port for us
            sock.bind(("", 0))
            port = sock.getsockname()[1]
            sock.close()
            # NOTE: there is still a chance the port could be taken by other processes.
            return port
        port = _find_free_port()
        setattr(opts, "ddp.dist_port", port)

        torch.multiprocessing.spawn(
            fn=distributed_worker,
            args=(main, opts, kwargs),
            nprocs=num_gpus,
        )
    else:
        if dataset_workers == -1:
            setattr(opts, "dataset.workers", n_cpus)

        if norm_name in ["sync_batch_norm", "sbn"]:
            setattr(opts, "model.normalization.name", "batch_norm")

        # adjust the batch size
        train_bsize = getattr(opts, "dataset.train_batch_size0", 32) * max(1, num_gpus)
        val_bsize = getattr(opts, "dataset.val_batch_size0", 32) * max(1, num_gpus)
        setattr(opts, "dataset.train_batch_size0", train_bsize)
        setattr(opts, "dataset.val_batch_size0", val_bsize)
        setattr(opts, "dev.device_id", None)
        main(opts=opts, **kwargs)


if __name__ == "__main__":
    #
    main_worker()
