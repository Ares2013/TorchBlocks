import os
import csv
import torch
from torchblocks.losses import KL
from torchblocks.metrics import MattewsCorrcoef
from torchblocks.trainer import TextClassifierTrainer
from torchblocks.callback import ModelCheckpoint, TrainLogger
from torchblocks.processor import TextClassifierProcessor, InputExample
from torchblocks.utils import seed_everything, dict_to_text, build_argparse
from torchblocks.utils import prepare_device, get_checkpoints
from transformers import BertForSequenceClassification, BertConfig, BertTokenizer
from transformers import WEIGHTS_NAME

MODEL_CLASSES = {
    'bert': (BertConfig, BertForSequenceClassification, BertTokenizer)
}

kl = KL()

def adv_project(grad, norm_type='inf', eps=1e-6):
    if norm_type == 'l2':
        direction = grad / (torch.norm(grad, dim=-1, keepdim=True) + eps)
    elif norm_type == 'l1':
        direction = grad.sign()
    else:
        direction = grad / (grad.abs().max(-1, keepdim=True)[0] + eps)
    return direction

class ColaProcessor(TextClassifierProcessor):
    def __init__(self, tokenizer, data_dir, logger, prefix):
        super().__init__(tokenizer=tokenizer, data_dir=data_dir, logger=logger, prefix=prefix)

    def get_labels(self):
        """See base class."""
        return ["0", "1"]

    def read_data(self, input_file):
        """Reads a json list file."""
        with open(input_file, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f, delimiter="\t", quotechar=None)
            lines = []
            for line in reader:
                lines.append(line)
            return lines

    def create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            guid = "%s-%s" % (set_type, i)
            text_a = line[3] if set_type != 'test' else line[1]
            label = line[1] if set_type != 'test' else None
            examples.append(
                InputExample(guid=guid, texts=[text_a,None], label=label))
        return examples


class AlumTrainer(TextClassifierTrainer):
    def __init__(self, args, metrics, logger, batch_input_keys,collate_fn=None):
        super().__init__(args=args,
                         metrics=metrics,
                         logger=logger,
                         batch_input_keys=batch_input_keys,
                         collate_fn=collate_fn)

    def _train_step(self, model, batch, optimizer):
        model.train()
        inputs = self.build_inputs(batch)
        outputs = model(**inputs)
        loss, logits = outputs[:2]
        if isinstance(model, torch.nn.DataParallel):
            embeds_init = model.module.bert.embeddings.word_embeddings(inputs['input_ids'])
        else:
            embeds_init = model.bert.embeddings.word_embeddings(inputs["input_ids"])
        input_mask = inputs['attention_mask'].to(embeds_init)
        delta = torch.zeros_like(embeds_init).normal_(0, 1) * self.args.adv_var * input_mask.unsqueeze(2)
        for astep in range(self.args.adv_K):
            delta.requires_grad_()
            inputs['inputs_embeds'] = delta + embeds_init
            inputs['input_ids'] = None
            adv_outputs = model(**inputs)
            adv_logits = adv_outputs[1]  # model outputs are always tuple in transformers (see doc)

            adv_loss = kl(adv_logits, logits.detach())
            delta_grad, = torch.autograd.grad(adv_loss, delta, only_inputs=True)
            adv_direct = adv_project(delta_grad, norm_type=self.args.adv_norm_type, eps=self.args.adv_gamma)

            inputs['inputs_embeds'] = embeds_init + adv_direct * self.args.adv_lr
            outputs = model(**inputs)
            adv_loss_f = kl(outputs[1], logits.detach())
            adv_loss_b = kl(logits, outputs[1].detach())
            adv_loss = (adv_loss_f + adv_loss_b) * self.args.adv_alpha
            loss = loss + adv_loss
            if self.args.n_gpu > 1:
                loss = loss.mean()  # mean() to average on multi-gpu parallel training
            if self.args.gradient_accumulation_steps > 1:
                loss = loss / self.args.gradient_accumulation_steps
            loss.backward()
            if isinstance(model, torch.nn.DataParallel):
                embeds_init = model.module.bert.embeddings.word_embeddings(batch[0])
            else:
                embeds_init = model.bert.embeddings.word_embeddings(batch[0])
        return loss.item()


def main():
    parser = build_argparse()
    parser.add_argument('--adv_lr', type=float, default=1e-3)
    parser.add_argument('--adv_K', type=int, default=1)
    parser.add_argument('--adv_alpha', default=1.0, type=float)
    parser.add_argument('--adv_var', default=1e-5, type=float)
    parser.add_argument('--adv_gamma', default=1e-6, type=float)
    parser.add_argument('--adv_norm_type', type=str, default="inf", choices=["l2", 'l1', "inf"])
    parser.add_argument('--hidden_dropout_prob', type=float, default=0.1)
    parser.add_argument('--attention_probs_dropout_prob', type=float, default=0)
    args = parser.parse_args()

    if args.model_name is None:
        args.model_name = args.model_path.split("/")[-1]
    args.output_dir = args.output_dir + '{}'.format(args.model_name)
    os.makedirs(args.output_dir, exist_ok=True)
    prefix = "_".join([args.model_name, args.task_name])
    logger = TrainLogger(log_dir=args.output_dir, prefix=prefix)

    logger.info("initializing device")
    args.device, args.n_gpu = prepare_device(args.gpu, args.local_rank)
    seed_everything(args.seed)
    args.model_type = args.model_type.lower()
    config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]

    logger.info("initializing data processor")
    tokenizer = tokenizer_class.from_pretrained(args.model_path, do_lower_case=args.do_lower_case)
    processor = ColaProcessor(tokenizer, args.data_dir, logger, prefix=prefix)
    label_list = processor.get_labels()
    num_labels = len(label_list)
    args.num_labels = num_labels

    logger.info("initializing model and config")
    config = config_class.from_pretrained(args.model_path,
                                          num_labels=num_labels,
                                          attention_probs_dropout_prob=args.attention_probs_dropout_prob,
                                          hidden_dropout_prob=args.hidden_dropout_prob,
                                          cache_dir=args.cache_dir if args.cache_dir else None)
    model = model_class.from_pretrained(args.model_path, config=config)
    model.to(args.device)


    logger.info("initializing traniner")
    trainer = AlumTrainer(logger=logger,
                          args=args,
                          batch_input_keys=processor.get_batch_keys(),
                          collate_fn=processor.collate_fn,
                          metrics=[MattewsCorrcoef()])
    if args.do_train:
        train_dataset = processor.create_dataset(max_seq_length=args.train_max_seq_length,
                                                 data_name='train.tsv', mode='train')
        eval_dataset = processor.create_dataset(max_seq_length=args.eval_max_seq_length,
                                                data_name='dev.tsv', mode='dev')
        trainer.train(model, train_dataset=train_dataset, eval_dataset=eval_dataset)

    if args.do_eval and args.local_rank in [-1, 0]:
        results = {}
        eval_dataset = processor.create_dataset(max_seq_length=args.eval_max_seq_length,
                                                data_name='dev.tsv', mode='dev')
        checkpoints = [args.output_dir]
        if args.eval_all_checkpoints or args.checkpoint_number > 0:
            checkpoints = get_checkpoints(args.output_dir, args.checkpoint_number, WEIGHTS_NAME)
        logger.info("Evaluate the following checkpoints: %s", checkpoints)
        for checkpoint in checkpoints:
            global_step = checkpoint.split("/")[-1].split("-")[-1]
            model = model_class.from_pretrained(checkpoint, config=config)
            model.to(args.device)
            trainer.evaluate(model, eval_dataset, save_preds=True, prefix=str(global_step))
            if global_step:
                result = {"{}_{}".format(global_step, k): v for k, v in trainer.records['result'].items()}
                results.update(result)
        output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
        dict_to_text(output_eval_file, results)

    if args.do_predict:
        test_dataset = processor.create_dataset(max_seq_length=args.eval_max_seq_length,
                                                data_name='test.tsv', mode='test')
        if args.checkpoint_number == 0:
            raise ValueError("checkpoint number should > 0,but get %d", args.checkpoint_number)
        checkpoints = get_checkpoints(args.output_dir, args.checkpoint_number, WEIGHTS_NAME)
        for checkpoint in checkpoints:
            global_step = checkpoint.split("/")[-1].split("-")[-1]
            model = model_class.from_pretrained(checkpoint)
            model.to(args.device)
            trainer.predict(model, test_dataset=test_dataset, prefix=str(global_step))


if __name__ == "__main__":
    main()
