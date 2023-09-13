r"""Library for evaluating active sampling.
"""

import os
from typing import Mapping

import numpy as np
import pandas as pd
from skai.model import data
import tensorflow as tf


def merge_subgroup_labels(
    ds: tf.data.Dataset,
    table: pd.DataFrame,
    batch_size: int,
):
  """Merge table with subgroup labels from the dataset.

    Args:
        ds (tf.data.Dataset): The dataset containing subgroup labels.
        table (pd.DataFrame): The table to merge with subgroup labels.
        batch_size (int): Batch size for processing the dataset.

    Returns:
        pd.DataFrame: The merged table with subgroup labels.

    """

  ids = np.concatenate(list(
      ds.map(lambda example: example['example_id']).batch(
          batch_size).as_numpy_iterator())).tolist()
  ids = list(map(lambda x: x.decode('UTF-8'), ids))
  subgroup_labels = np.concatenate(list(
      ds.map(lambda example: example['subgroup_label']).batch(
          batch_size).as_numpy_iterator())).tolist()
  labels = np.concatenate(list(
      ds.map(lambda example: example['label']).batch(
          batch_size).as_numpy_iterator())).tolist()
  df_a = pd.DataFrame({
      'example_id': ids, 'subgroup_label': subgroup_labels,
      'label': labels})
  table = table[table['example_id'].isin(ids)]
  return pd.merge(table, df_a, on=['example_id'])


def _process_table(table: pd.DataFrame, prediction: bool):
  """Modify the table to have cleaned up example IDs and predictions.

    Args:
        table (pd.DataFrame): The table to process.
        prediction (bool): Whether to process predictions.

    Returns:
        pd.DataFrame: The processed table.

    """
  table['example_id'] = table['example_id'].map(
      lambda x: eval(x).decode('UTF-8'))  #  pylint:disable=eval-used
  if prediction:
    prediction_label_cols = filter(lambda x: 'label' in x, table.columns)
    prediction_bias_cols = filter(lambda x: 'bias' in x, table.columns)
    table['bias'] = table[prediction_bias_cols].mean(axis=1)
    table['label_prediction'] = table[prediction_label_cols].mean(axis=1)
  return table


def evaluate_active_sampling(
    num_rounds: int,
    output_dir: str,
    dataloader: data.Dataloader,
    batch_size: int,
    num_subgroups: int,
    ) -> pd.DataFrame:
  """Evaluates model for subgroup representation vs number of rounds.

    Args:
        num_rounds (int): The number of evaluation rounds.
        output_dir (str): The directory where the evaluation results are stored.
        dataloader (data.Dataloader): The DataLoader instance containing the dataset.
        batch_size (int): Batch size for processing the dataset.
        num_subgroups (int): The number of subgroups in the dataset.

    Returns:
        pd.DataFrame: A DataFrame containing the evaluation results, including the
            number of samples, probability representation, round index, and subgroup IDs.

    """

  round_idx = []
  subgroup_ids = []
  num_samples = []
  prob_representation = []
  for idx in range(num_rounds):
    ds = dataloader.train_ds
    bias_table = pd.read_csv(
        os.path.join(
            os.path.join(output_dir, f'round_{idx}'), 'bias_table.csv'))
    predictions_merge = merge_subgroup_labels(ds, bias_table, batch_size)
    for subgroup_id in range(num_subgroups):
      prob_i = (predictions_merge['subgroup_label']
                == subgroup_id).sum() / len(predictions_merge)
      round_idx.append(idx)
      subgroup_ids.append(subgroup_id)
      num_samples.append(len(predictions_merge))
      prob_representation.append(prob_i)
  return pd.DataFrame({
      'num_samples': num_samples,
      'prob_representation': prob_representation,
      'round_idx': round_idx,
      'subgroup_ids': subgroup_ids,
  })


def evaluate_model(
    round_idx: int,
    output_dir: str,
    dataloader: data.Dataloader,
    batch_size: int,
    ) -> Mapping[str, pd.DataFrame]:
  """Evaluates the model for subgroup representation versus number of rounds.

    Args:
        round_idx (int): The current round index.
        output_dir (str): The output directory where evaluation files are stored.
        dataloader (data.Dataloader): The data loader containing datasets.
        batch_size (int): Batch size for processing the datasets.

    Returns:
        Mapping[str, pd.DataFrame]: A mapping of dataset names to dataframes
        containing merged subgroup labels and predictions.

    """
  bias_table = pd.read_csv(
      os.path.join(
          os.path.join(output_dir, f'round_{round_idx}'), 'bias_table.csv'))
  bias_table = _process_table(bias_table, False)
  predictions_table = pd.read_csv(
      os.path.join(
          os.path.join(output_dir, f'round_{round_idx}'),
          'predictions_table.csv'))
  predictions_table = _process_table(predictions_table, True)
  predictions_merge = {}
  predictions_merge['train_bias'] = merge_subgroup_labels(
      dataloader.train_ds, bias_table, batch_size)
  predictions_merge['train_predictions'] = merge_subgroup_labels(
      dataloader.train_ds, predictions_table, batch_size)
  for (ds_name, ds) in dataloader.eval_ds.items():
    predictions_table = _process_table(pd.read_csv(
        os.path.join(
            os.path.join(output_dir, f'round_{round_idx}'),
            f'predictions_table_{ds_name}.csv')), True)
    predictions_merge[f'{ds_name}_predictions'] = merge_subgroup_labels(
        ds, predictions_table, batch_size)
    predictions_merge[f'{ds_name}_bias'] = merge_subgroup_labels(
        ds, bias_table, batch_size)
  return predictions_merge
