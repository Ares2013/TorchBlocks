import torch
import torch.nn as nn
from .base import TrainerBase
from ..callback import ProgressBar
from ..metrics.ner_utils import get_spans
from ..utils.tensor import tensor_to_list


class SequenceLabelingTrainer(TrainerBase):
    '''
    Sequence Labeling crf or softmax Trainer
    '''

    def __init__(self, args, metrics, logger, batch_input_keys, collate_fn=None):
        super().__init__(args=args,
                         metrics=metrics,
                         logger=logger,
                         batch_input_keys=batch_input_keys,
                         collate_fn=collate_fn)

    def evaluate(self, model, eval_dataset, save_preds=False, prefix=''):
        eval_dataloader = self.build_eval_dataloader(eval_dataset)
        self._predict_forward(model, eval_dataloader, do_eval=True)
        for i, label in enumerate(self.records['target']):
            temp_1 = []
            temp_2 = []
            for j, m in enumerate(label):
                if j == 0:
                    continue
                elif j == self.records['input_lens'][i] - 1:  # 实际长度
                    self.metrics[0].update(input=[temp_2], target=[temp_1])
                    break
                else:
                    temp_1.append(self.records['target'][i][j])
                    temp_2.append(self.records['preds'][i][j])
        self.logger.info("   ")
        value, entity_value = self.metrics[0].value()
        self.records['result']['eval_loss'] = self.records['loss_meter'].avg
        self.records['result'].update({f"eval_{k}": f"{v:.5f}" for k, v in value.items()})

        if save_preds:
            output_logits_file = f"{self.prefix + prefix}_predict_eval_logits.pkl"
            self.save_predict_result(file_name=output_logits_file,
                                     data=self.records['preds'])
        self.print_evaluate_result()
        self.print_label_result(entity_value)

        if 'cuda' in str(self.args.device):
            torch.cuda.empty_cache()

    def print_label_result(self, entity_value):
        self.logger.info("***** Eval label results of %s *****", self.args.task_name)
        for key in sorted(entity_value.keys()):
            self.logger.info(f" {key} result: ")
            info = "-".join([f' {key}: {value:.4f} ' for key, value in entity_value[key].items()])
            self.logger.info(info)

    def predict(self, model, test_dataset, prefix=''):
        test_dataloader = self.build_test_dataloader(test_dataset)
        self._predict_forward(model, test_dataloader, do_eval=False)
        results = []
        for i, pred in enumerate(self.records['preds']):
            pred = pred[:self.records['input_lens'][i]][1:-1]  # [CLS]XXXX[SEP]
            entity_spans = get_spans(pred, self.args.id2label, self.args.markup)
            json_d = {}
            json_d['id'] = i
            json_d['tag_sequence'] = " ".join([self.args.id2label[x] for x in pred])
            json_d['entities'] = entity_spans
            results.append(json_d)

        output_predict_file = f"{self.prefix + prefix}_predict_test.json"
        self.save_predict_result(file_name=output_predict_file,
                                 file_dir=self.args.output_dir,
                                 data=results)

        output_logits_file = f"{self.prefix + prefix}_predict_test_logits.pkl"
        self.save_predict_result(file_name=output_logits_file,
                                 file_dir=self.args.output_dir,
                                 data=self.records['preds'])

    def _predict_forward(self, model, data_loader, do_eval=True, **kwargs):
        self.build_record_object()
        pbar = ProgressBar(n_total=len(data_loader), desc='Evaluating' if do_eval else 'Predicting')
        for step, batch in enumerate(data_loader):
            model.eval()
            inputs = self.build_inputs(batch)
            with torch.no_grad():
                outputs = model.module(**inputs) if isinstance(model, nn.DataParallel) else model(**inputs)
            if do_eval:
                loss, logits = outputs[:2]
                self.records['loss_meter'].update(loss.item(), n=1)
                self.records['target'].extend(tensor_to_list(inputs['labels']))
            else:
                logits = outputs[0]
            if self.args.use_crf:
                tags = model.crf.decode(logits, inputs['attention_mask'])
                self.records['preds'].extend(tensor_to_list(tags.squeeze(0)))
            else:
                self.records['preds'].extend(tensor_to_list(torch.argmax(logits, dim=2)))
            self.records['input_lens'].extend(tensor_to_list(torch.sum(inputs['attention_mask'], 1)).cpu())
            pbar(step)


class SequenceLabelingSpanTrainer(TrainerBase):
    '''
    Sequence Labeling Span Trainer
    '''

    def __init__(self, args, metrics, logger, batch_input_keys, collate_fn=None):
        super().__init__(args=args,
                         metrics=metrics,
                         logger=logger,
                         batch_input_keys=batch_input_keys,
                         collate_fn=collate_fn)

    def extract_items(self, start_, end_, length):
        items = []
        start_ = start_[:length][1:-1]  # 实际长度
        end_ = end_[:length][1:-1]
        for i, s_l in enumerate(start_):
            if s_l == 0:
                continue
            for j, e_l in enumerate(end_[i:]):
                if s_l == e_l:
                    items.append((s_l, i, i + j))
                    break
        return items

    def print_label_result(self, entity_value):
        self.logger.info("***** Eval label results of %s *****", self.args.task_name)
        for key in sorted(entity_value.keys()):
            self.logger.info(f" {key} result: ")
            info = "-".join([f' {key}: {value:.4f} ' for key, value in entity_value[key].items()])
            self.logger.info(info)

    def evaluate(self, model, eval_dataset, save_preds=False, prefix=''):
        eval_dataloader = self.build_eval_dataloader(eval_dataset)
        self._predict_forward(model, eval_dataloader, do_eval=True)
        for i in range(len(self.records['preds'])):
            length = self.records['input_lens'][i]
            start_logits, end_logits = self.records['preds'][i]
            start_positions, end_positions = self.records['target'][i]
            R = self.extract_items(start_logits, end_logits, length)
            T = self.extract_items(start_positions, end_positions, length)
            self.metrics[0].update(input=R, target=T)
        self.logger.info("   ")
        value, entity_value = self.metrics[0].value()
        self.records['result']['eval_loss'] = self.records['loss_meter'].avg
        self.records['result'].update({f"eval_{k}": f"{v:.5f}" for k, v in value.items()})

        if save_preds:
            output_logits_file = f"{self.prefix + prefix}_predict_eval_logits.pkl"
            self.save_predict_result(file_name=output_logits_file,
                                     data=self.records['preds'])
        self.print_evaluate_result()
        self.print_label_result(entity_value)

        if 'cuda' in str(self.args.device):
            torch.cuda.empty_cache()

    def predict(self, model, test_dataset, prefix=''):
        test_dataloader = self.build_test_dataloader(test_dataset)
        self._predict_forward(model, test_dataloader, do_eval=False)
        results = []
        for i, (start_logits, end_logits) in enumerate(self.records['preds']):
            length = self.records['input_lens'][i]
            entity_spans = self.extract_items(start_logits, end_logits, length)
            if entity_spans:
                entity_spans = [[self.args.id2label[x[0]], x[1], x[2]] for x in entity_spans]
            json_d = {}
            json_d['id'] = i
            json_d['entities'] = entity_spans
            results.append(json_d)

        output_predict_file = f"{self.prefix + prefix}_predict_test.json"
        self.save_predict_result(file_name=output_predict_file,
                                 data=results)

        output_logits_file = f"{self.prefix + prefix}_predict_test_logits.pkl"
        self.save_predict_result(file_name=output_logits_file,
                                 data=self.records['preds'])

    def _predict_forward(self, model, data_loader, do_eval=True, **kwargs):
        self.build_record_object()
        pbar = ProgressBar(n_total=len(data_loader), desc='Evaluating' if do_eval else 'Predicting')
        for step, batch in enumerate(data_loader):
            model.eval()
            inputs = self.build_inputs(batch)
            with torch.no_grad():
                outputs = model(**inputs)
            if do_eval:
                loss, start_logits, end_logits = outputs[:3]
                self.records['loss_meter'].update(loss.item(), n=1)
                start_positions = tensor_to_list(inputs['start_positions'])
                end_positions = tensor_to_list(inputs['end_positions'])
                self.records['target'].extend(zip(start_positions, end_positions))
            else:
                start_logits, end_logits = outputs[:2]
            start_logits = tensor_to_list(torch.argmax(start_logits, -1))
            end_logits = tensor_to_list(torch.argmax(end_logits, -1))
            self.records['preds'].extend(zip(start_logits, end_logits))
            self.records['input_lens'].extend(tensor_to_list(torch.sum(inputs['attention_mask'], 1)))
            pbar(step)
