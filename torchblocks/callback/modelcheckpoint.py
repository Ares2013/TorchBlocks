'''
Automatically save model checkpoints during training.
'''
import os
import torch
import numpy as np
import logging

logger = logging.getLogger(__name__)

DEFAULT_SAVE_MODEL_NAME = 'checkpoint'


class ModelCheckpoint(object):
    def __init__(self,checkpoint_dir,monitor,
                 mode='min',
                 save_best_only=False):

        checkpoint_dir = checkpoint_dir
        os.makedirs(checkpoint_dir, exist_ok=True)

        self.base_path = checkpoint_dir
        self.monitor = monitor
        self.save_best_only = save_best_only
        if mode == 'min':
            self.monitor_op = np.less
            self.best = np.Inf
        elif mode == 'max':
            self.monitor_op = np.greater
            self.best = -np.Inf
        if save_best_only:
            self.output_dir = os.path.join(checkpoint_dir, f"{DEFAULT_SAVE_MODEL_NAME}-best")
            os.makedirs(self.output_dir, exist_ok=True)
        else:
            self.output_dir = os.path.join(checkpoint_dir, f"{DEFAULT_SAVE_MODEL_NAME}-%s")

    def save_checkpoint(self, state, save_dir):
        if not os.path.exists(save_dir):
            os.mkdir(save_dir)

        assert 'model' in state
        logger.info("Saving model checkpoint to %s", save_dir)
        model = state['model']
        if hasattr(model, 'save'):
            model.save(save_dir)
        if hasattr(model, 'save_pretrained'):
            model.save_pretrained(save_dir)
        state.pop('model')

        torch.save(state['args'], os.path.join(save_dir, "training_args.bin"))
        state.pop('args')

        if state.get('optimizer', None):
            logger.info("Saving optimizer and scheduler states to %s", save_dir)
            torch.save(state['optimizer'].state_dict(), os.path.join(save_dir, "optimizer.pt"))
            state.pop('optimizer')

        if state.get('scheduler', None):
            torch.save(state['scheduler'].state_dict(), os.path.join(save_dir, "scheduler.pt"))
            state.pop('scheduler')

        logger.info("Saving states to %s", save_dir)
        torch.save(state, os.path.join(save_dir, "state.bin"))

    def step(self, state, current):

        if self.save_best_only:
            if self.monitor_op(current, self.best):
                logger.info(
                    f" Steps {state['step']}: {self.monitor} improved from {self.best:.5f} to {current:.5f}")
                self.best = current
                state['best'] = self.best
                self.save_checkpoint(state, self.output_dir)
        else:
            output_dir = self.output_dir % state['step']
            if not os.path.exists(output_dir):
                os.mkdir(output_dir)
                logger.info(f" Step {state['step']} - {self.monitor}: {current:.5f} save model to disk.")
                self.save_checkpoint(state, output_dir)
