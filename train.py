# coding: utf-8

"""
The ``train.py`` file can be used to train a model.
It requires a configuration file and a directory in
which to write the results.

.. code-block:: bash

   $ python train.py --help
    usage: train.py [-h] -s SERIALIZATION_DIR -c CONFIG_FILE_PATH [-r]

    optional arguments:
    -h, --help            show this help message and exit
    -s SERIALIZATION_DIR, --serialization_dir SERIALIZATION_DIR
                            Directory in which to save the model and its logs.
    -c CONFIG_FILE_PATH, --config_file_path CONFIG_FILE_PATH
                            Path to parameter file describing the multi-tasked
                            model to be trained.
    -r, --recover         Recover a previous training from the state in
                            serialization_dir.
"""

import argparse
import itertools
import os
import json
from copy import deepcopy
import torch
from typing import List, Dict, Any, Tuple


from mtl.logger import logger
from mtl.multi_task_trainer import MultiTaskTrainer

TASKS_NAME = ["beauty",
              "catering", 
              "education",
              "efficiency",
              "finance",
              "game",
              "health",
              "life",
              "local",
              "management",
              "recreation",
              "shopping",
              "sports",
              "video"
              ]


from evaluate import evaluate
from mtl.task import Task
from mtl.util import create_and_set_iterators

from allennlp.models.model import Model
from allennlp.data import Vocabulary
from allennlp.commands.train import create_serialization_dir
from allennlp.common.params import Params
from allennlp.nn import RegularizerApplicator


def tasks_and_vocab_from_params(params: Params, serialization_dir: str) -> Tuple[List[Task], Vocabulary]:
    """
    Load each of the tasks in the model from the ``params`` file
    and load the datasets associated with each of these tasks.
    Create the vocavulary from ``params`` using the concatenation of the ``datasets_for_vocab_creation``
    from each of the tasks specific dataset.

    Parameters
    ----------
    params: ``Params``
        A parameter object specifing an experiment.
    serialization_dir: ``str``
        Directory in which to save the model and its logs.
    Returns
    -------
    task_list: ``List[Task]``
        A list containing the tasks of the model to train.
    vocab: ``Vocabulary``
        The vocabulary fitted on the datasets_for_vocab_creation.
    """
    ### Instantiate the different tasks ###
    task_list = []
    instances_for_vocab_creation = itertools.chain()
    datasets_for_vocab_creation = {}
    task_keys = TASKS_NAME
    data_dir = "data/mtl-dataset-total"

    for key in task_keys:
        logger.info("Creating {}", key)
        task_data_params = Params({
            "dataset_reader": {
                "type": "semantic_review"
            },
            "train_data_path": os.path.join(data_dir, key + ".task.train"),
            "test_data_path": os.path.join(data_dir, key + ".task.test"),
            "validation_data_path": os.path.join(data_dir, key + ".task.test"),
            "datasets_for_vocab_creation": [
                "train",
                "validation",
                "test"
            ]
        })

        task_description = Params({"task_name": key, "validation_metric_name": "acc"})

        task = Task.from_params(params=task_description)
        task_list.append(task)

        task_instances_for_vocab, task_datasets_for_vocab = task.load_data_from_params(params=task_data_params)
        instances_for_vocab_creation = itertools.chain(instances_for_vocab_creation, task_instances_for_vocab)
        datasets_for_vocab_creation[task._name] = task_datasets_for_vocab

    ### Create and save the vocabulary ###
    for task_name, task_dataset_list in datasets_for_vocab_creation.items():
        logger.info("Creating a vocabulary using {} data from {}.", ", ".join(task_dataset_list), task_name)

    logger.info("Fitting vocabulary from dataset")
    vocab = Vocabulary.from_params(params.pop("vocabulary", {}), instances_for_vocab_creation)

    vocab.save_to_files(os.path.join(serialization_dir, "vocabulary"))
    logger.info("Vocabulary saved to {}", os.path.join(serialization_dir, "vocabulary"))

    return task_list, vocab


def train_model(multi_task_trainer: MultiTaskTrainer, recover: bool = False) -> Dict[str, Any]:
    """
    Launching the training of the multi-tasks model.

????????Parameters
????????----------
    multi_task_trainer: ``MultiTaskTrainer``
????????????????A trainer (similar to allennlp.training.trainer.Trainer) that can handle multi-tasks training.
    recover : ``bool``, optional (default=False)
        If ``True``, we will try to recover a training run from an existing serialization
        directory.  This is only intended for use when something actually crashed during the middle
        of a run.  For continuing training a model on new data, see the ``fine-tune`` command.
                ????????
????????Returns
????????-------
    metrics: ``Dict[str, Any]
        The different metrics summarizing the training of the model.
        It includes the validation and test (if necessary) metrics.
    """
    ### Train the multi-tasks model ###
    metrics = multi_task_trainer.train(recover=recover)

    task_list = multi_task_trainer._task_list
    serialization_dir = multi_task_trainer._serialization_dir
    model = multi_task_trainer._model

    # Evaluate the model on test data. if necessary This is a multi-tasks learning framework, the best validation
    # metrics for one tasks are not necessarily obtained from the same epoch for all the tasks, one epoch begin equal
    # to N forward+backward passes, where N is the total number of batches in all the training sets. We evaluate each
    # of the best model for each tasks (based on the validation metrics) for all the other tasks (which have a test
    # set).
    avg_accuracies = []
    for task in task_list:
        if not task._evaluate_on_test:
            continue

        logger.info("Task {} will be evaluated using the best epoch weights.", task._name)
        assert (
                task._test_data is not None
        ), "Task {} wants to be evaluated on test dataset but no there is no test data loaded.".format(task._name)

        logger.info("Loading the best epoch weights for tasks {}", task._name)
        best_model_state_path = os.path.join(serialization_dir, "best_{}.th".format(task._name))
        best_model_state = torch.load(best_model_state_path)
        best_model = model
        best_model.load_state_dict(state_dict=best_model_state)

        test_metric_dict = {}
        avg_accuracy = 0.0
        with open("class_probabilities.txt", "a", encoding="utf8") as f:
            f.write(f"Evaluate task : {task._name}\n")
        with open("attn.txt", "a", encoding="utf8") as f:
            f.write(f"Evaluate task : {task._name}\n")
        for pair_task in task_list:
            if not pair_task._evaluate_on_test:
                continue

            logger.info("Pair tasks {} is evaluated with the best model for {}", pair_task._name, task._name)
            test_metric_dict[pair_task._name] = {}
            test_metrics = evaluate(
                model=best_model,
                task_name=pair_task._name,
                instances=pair_task._test_data,
                data_iterator=pair_task._data_iterator,
                cuda_device=multi_task_trainer._cuda_device,
            )

            for metric_name, value in test_metrics.items():
                test_metric_dict[pair_task._name][metric_name] = value
            avg_accuracy += test_metrics["acc"]
        logger.info("Average accuracy of task {} is {}", task._name, avg_accuracy / len(task_list))
        avg_accuracies.append(avg_accuracy / len(task_list))

        metrics[task._name]["test"] = deepcopy(test_metric_dict)
        logger.info("Finished evaluation of tasks {}.", task._name)

    ### Dump validation and possibly test metrics ###
    metrics_json = json.dumps(metrics, indent=2)
    with open(os.path.join(serialization_dir, "metrics.json"), "w") as metrics_file:
        metrics_file.write(metrics_json)
    logger.info("Metrics: {}", metrics_json)
    logger.info("Average accuracy is {}", sum(avg_accuracies) / len(avg_accuracies))

    return metrics


if __name__ == "__main__":
    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-s",
        "--serialization_dir",
        required=True,
        help="Directory in which to save the model and its logs.",
        type=str,
    )
    parser.add_argument(
        "-c",
        "--config_file_path",
        required=True,
        help="Path to parameter file describing the multi-tasked model to be trained.",
        type=str,
    )
    parser.add_argument(
        "-r",
        "--recover",
        action="store_true",
        help="Recover a previous training from the state in serialization_dir.",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
    )
    parser.add_argument(
        '--fp16',
        action='store_true',
        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument(
        '-ls',
        '--loss_scale',
        type=float, default=0,
        help="Loss scaling to improve fp16 numeric stability. Only used when fp16 set to True.\n"
             "0 (default value): dynamic loss scaling.\n"
             "Positive power of 2: static loss scaling value.\n")
    args = parser.parse_args()

    params = Params.from_file(params_file=args.config_file_path)
    serialization_dir = args.serialization_dir
    create_serialization_dir(params, serialization_dir, args.recover, args.force)

    serialization_params = deepcopy(params).as_dict(quiet=True)
    with open(os.path.join(serialization_dir, "config.json"), "w") as param_file:
        json.dump(serialization_params, param_file, indent=4)

    ### Instantiate the different tasks from the param file, load datasets and create vocabulary ###
    tasks, vocab = tasks_and_vocab_from_params(params=params, serialization_dir=serialization_dir)

    ### Load the data iterators for each tasks ###
    tasks = create_and_set_iterators(params=params, task_list=tasks, vocab=vocab)

    ### Load Regularizations ###
    regularizer = RegularizerApplicator.from_params(params.pop("regularizer", []))

    ### Create model ###
    model_params = params.pop("model")
    model = Model.from_params(vocab=vocab, params=model_params, regularizer=regularizer)
    ### Create multi-tasks trainer ###
    multi_task_trainer_params = params.pop("multi_task_trainer")
    trainer = MultiTaskTrainer.from_params(
        model=model, task_list=tasks, serialization_dir=serialization_dir, params=multi_task_trainer_params
    )

    ### Launch training ###
    metrics = train_model(multi_task_trainer=trainer, recover=args.recover)
    if metrics is not None:
        logger.info("Training is finished ! Let's have a drink. It's on the house !")
