# Copyright 2017 Yahoo Inc.
# Licensed under the terms of the Apache 2.0 license.
# Please see LICENSE file in the project root for terms.
"""
This module provides a high-level API to manage the TensorFlowOnSpark cluster.

There are three main phases of operation:

1. **Reservation/Startup** - reserves a port for the TensorFlow process on each executor, starts a multiprocessing.Manager to
   listen for data/control messages, and then launches the Tensorflow main function on the executors.

2. **Data feeding** - *For InputMode.SPARK only*. Sends RDD data to the TensorFlow nodes via each executor's multiprocessing.Manager.  PS
   nodes will tie up their executors, so they won't receive any subsequent data feeding tasks.

3. **Shutdown** - sends a shutdown control message to the multiprocessing.Managers of the PS nodes and pushes end-of-feed markers into the data
   queues of the worker nodes.

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import nested_scopes
from __future__ import print_function

import logging
import os
import random
import sys
import threading
import time
from pyspark.streaming import DStream
from hops import hdfs as hopshdfs
from hops import util
import atexit
from datetime import datetime
import json

from . import reservation
from . import TFManager
from . import TFSparkNode

# status of TF background job
tf_status = {}

elastic_id = 0
experiment_json = None
running = False
app_id = None
run_id = 0

class InputMode(object):
  """Enum for the input modes of data feeding."""
  TENSORFLOW = 0                #: TensorFlow application is responsible for reading any data.
  SPARK = 1                     #: Spark is responsible for feeding data to the TensorFlow application via an RDD.


class TFCluster(object):

  sc = None
  defaultFS = None
  working_dir = None
  num_executors = None
  nodeRDD = None
  cluster_id = None
  cluster_info = None
  cluster_meta = None
  input_mode = None
  queues = None
  server = None

  def train(self, dataRDD, num_epochs=0, qname='input'):
    """*For InputMode.SPARK only*.  Feeds Spark RDD partitions into the TensorFlow worker nodes

    It is the responsibility of the TensorFlow "main" function to interpret the rows of the RDD.

    Since epochs are implemented via ``RDD.union()`` and the entire RDD must generally be processed in full, it is recommended
    to set ``num_epochs`` to closely match your training termination condition (e.g. steps or accuracy).  See ``TFNode.DataFeed``
    for more details.

    Args:
      :dataRDD: input data as a Spark RDD.
      :num_epochs: number of times to repeat the dataset during training.
      :qname: *INTERNAL USE*.
    """
    logging.info("Feeding training data")
    assert(self.input_mode == InputMode.SPARK)
    assert(qname in self.queues)
    assert(num_epochs >= 0)

    if isinstance(dataRDD, DStream):
      # Spark Streaming
      dataRDD.foreachRDD(lambda rdd: rdd.foreachPartition(TFSparkNode.train(self.cluster_info, self.cluster_meta, qname)))
    else:
      # Spark RDD
      # if num_epochs unspecified, pick an arbitrarily "large" number for now
      # TODO: calculate via dataRDD.count() / batch_size / max_steps
      if num_epochs == 0:
        num_epochs = 10
      rdds = []
      for i in range(num_epochs):
        rdds.append(dataRDD)
      unionRDD = self.sc.union(rdds)
      unionRDD.foreachPartition(TFSparkNode.train(self.cluster_info, self.cluster_meta, qname))

  def get_logdir(app_id, run_id):
    return hopshdfs.get_experiments_dir() + '/' + app_id + '/tensorflowonspark/run.' + str(run_id)

  def inference(self, dataRDD, qname='input'):
    """*For InputMode.SPARK only*: Feeds Spark RDD partitions into the TensorFlow worker nodes and returns an RDD of results

    It is the responsibility of the TensorFlow "main" function to interpret the rows of the RDD and provide valid data for the output RDD.

    This will use the distributed TensorFlow cluster for inferencing, so the TensorFlow "main" function should be capable of inferencing.
    Per Spark design, the output RDD will be lazily-executed only when a Spark action is invoked on the RDD.

    Args:
      :dataRDD: input data as a Spark RDD
      :qname: *INTERNAL_USE*

    Returns:
      A Spark RDD representing the output of the TensorFlow inferencing
    """
    logging.info("Feeding inference data")
    assert(self.input_mode == InputMode.SPARK)
    assert(qname in self.queues)
    return dataRDD.mapPartitions(TFSparkNode.inference(self.cluster_info, qname))

  def shutdown(self, ssc=None):
    """Stops the distributed TensorFlow cluster.

    Args:
      :ssc: *For Streaming applications only*. Spark StreamingContext
    """
    logging.info("Stopping TensorFlow nodes")

    # identify ps/workers
    ps_list, worker_list = [], []
    for node in self.cluster_info:
      if node['job_name'] == 'ps':
        ps_list.append(node)
      else:
        worker_list.append(node)

    if ssc is not None:
      # Spark Streaming
      done = False
      while not done:
        done = ssc.awaitTerminationOrTimeout(1)
        if not done and self.server.done:
          logging.info("Server done, stopping StreamingContext")
          ssc.stop(stopSparkContext=False, stopGraceFully=True)
        done = done or self.server.done
    else:
      # in TENSORFLOW mode, there is no "data feeding" job, only a "start" job, so we must wait for the TensorFlow workers
      # to complete all tasks, while accounting for any PS tasks which run indefinitely.
      if self.input_mode == InputMode.TENSORFLOW:
        count = 0
        done = False
        while not done:
          st = self.sc.statusTracker()
          jobs = st.getActiveJobsIds()
          if len(jobs) > 0:
            stages = st.getActiveStageIds()
            for i in stages:
              si = st.getStageInfo(i)
              if si.numActiveTasks == len(ps_list):
                # if we only have PS tasks left, check that we see this condition a couple times
                count += 1
                done = (count >= 3)
                time.sleep(5)
          else:
            done = True
            global running
            running = False



      # shutdown queues and managers for "worker" executors.
      # note: in SPARK mode, this job will immediately queue up behind the "data feeding" job.
      # in TENSORFLOW mode, this will only run after all workers have finished.
      workers = len(worker_list)
      workerRDD = self.sc.parallelize(range(workers), workers)
      workerRDD.foreachPartition(TFSparkNode.shutdown(self.cluster_info, self.queues))

    # exit Spark application w/ err status if TF job had any errors
    if 'error' in tf_status:
      logging.error("Exiting Spark application with error status.")
      exception_handler()
      self.sc.cancelAllJobs()
      #self.sc.stop()
      #sys.exit(1)
    global experiment_json
    global app_id
    experiment_json = util.finalize_experiment(experiment_json, None, None)

    util.put_elastic(hopshdfs.project_name(), app_id, str('dist' + str(elastic_id)), experiment_json)


    logging.info("Shutting down cluster")
    # shutdown queues and managers for "PS" executors.
    # note: we have to connect/shutdown from the spark driver, because these executors are "busy" and won't accept any other tasks.
    for node in ps_list:
      addr = node['addr']
      authkey = node['authkey']
      m = TFManager.connect(addr, authkey)
      q = m.get_queue('control')
      q.put(None)
      q.join()

    # wait for all jobs to finish
    done = False
    while not done:
      time.sleep(5)
      st = self.sc.statusTracker()
      jobs = st.getActiveJobsIds()
      if len(jobs) == 0:
        break

    def tensorboard_url(self):
      """
      Utility function to get Tensorboard URL
      """
      tb_url = None
      for node in self.cluster_info:
        if node['tb_port'] != 0 and node['job_name'] == 'worker' and node['task_index'] == 0:
          tb_url = "http://{0}:{1}".format(node['host'], node['tb_port'])
      return tb_url

def get_logdir(app_id):
  global run_id
  return hopshdfs.get_experiments_dir() + '/' + app_id + '/tensorflowonspark/run.' + str(run_id)

def run(sc, map_fun, tf_args, num_executors, num_ps, tensorboard=False, input_mode=InputMode.TENSORFLOW,
        log_dir=None, driver_ps_nodes=False, master_node=None, reservation_timeout=600, name='no-name', local_logdir=False, versioned_resources=None, description=None,
        queues=['input', 'output', 'error']):
  """Starts the TensorFlowOnSpark cluster and Runs the TensorFlow "main" function on the Spark executors

  Args:
    :sc: SparkContext
    :map_fun: user-supplied TensorFlow "main" function
    :tf_args: ``argparse`` args, or command-line ``ARGV``.  These will be passed to the ``map_fun``.
    :num_executors: number of Spark executors.  This should match your Spark job's ``--num_executors``.
    :num_ps: number of Spark executors which are reserved for TensorFlow PS nodes.  All other executors will be used as TensorFlow worker nodes.
    :tensorboard: boolean indicating if the chief worker should spawn a Tensorboard server.
    :input_mode: TFCluster.InputMode
    :log_dir: directory to save tensorboard event logs.  If None, defaults to a fixed path on local filesystem.
    :driver_ps_nodes: run the PS nodes on the driver locally instead of on the spark executors; this help maximizing computing resources (esp. GPU). You will need to set cluster_size = num_executors + num_ps
    :master_node: name of the "master" or "chief" node in the cluster_template, used for `tf.estimator` applications.
    :reservation_timeout: number of seconds after which cluster reservation times out (600 sec default)
    :queues: *INTERNAL_USE*

  Returns:
    A TFCluster object representing the started cluster.
  """

  #in hopsworks we want the tensorboard to always be true:
  global elastic_id
  global running
  global run_id
  tb=True
  elastic_id = elastic_id + 1
  run_id = run_id + 1
  running = True

  logging.info("Reserving TFSparkNodes {0}".format("w/ TensorBoard" if tb else ""))
  assert num_ps < num_executors

  if driver_ps_nodes:
    raise Exception('running PS nodes on driver is not supported and not needed on Hops Hadoop, since we have GPU scheduling.')

  if log_dir:
    raise Exception('No need to specify log_dir directory, we save TensorBoard events in the directory returned by tensorboard.logdir for you')

  # build a cluster_spec template using worker_nums
  cluster_template = {}
  cluster_template['ps'] = range(num_ps)
  if master_node is None:
    cluster_template['worker'] = range(num_ps, num_executors)
  else:
    cluster_template[master_node] = range(num_ps, num_ps + 1)
    if num_executors > num_ps + 1:
      cluster_template['worker'] = range(num_ps + 1, num_executors)
  logging.info("cluster_template: {}".format(cluster_template))

  # get default filesystem from spark
  defaultFS = sc._jsc.hadoopConfiguration().get("fs.defaultFS")
  # strip trailing "root" slash from "file:///" to be consistent w/ "hdfs://..."
  if defaultFS.startswith("file://") and len(defaultFS) > 7 and defaultFS.endswith("/"):
    defaultFS = defaultFS[:-1]

  # get current working dir of spark launch
  working_dir = os.getcwd()

  # start a server to listen for reservations and broadcast cluster_spec
  server = reservation.Server(num_executors)
  server_addr = server.start()

  # start TF nodes on all executors
  logging.info("Starting TensorFlow on executors")
  cluster_meta = {
    'id': random.getrandbits(64),
    'cluster_template': cluster_template,
    'num_executors': num_executors,
    'default_fs': defaultFS,
    'working_dir': working_dir,
    'server_addr': server_addr
  }


  nodeRDD = sc.parallelize(range(num_executors), num_executors)
  global app_id
  app_id = sc.applicationId
  global experiment_json

  versioned_path = util.version_resources(versioned_resources, get_logdir(app_id))

  experiment_json = None
  experiment_json = util.populate_experiment(sc, name, 'TFCluster', 'run', get_logdir(app_id), None, versioned_path, description)

  util.put_elastic(hopshdfs.project_name(), app_id, str('dist' + str(elastic_id)), experiment_json)

  # start TF on a background thread (on Spark driver) to allow for feeding job

  def _start(status):
    try:
      nodeRDD.foreachPartition(TFSparkNode.run(map_fun,
                                             tf_args,
                                             cluster_meta,
                                             tb,
                                             None,
                                             app_id,
                                             run_id,
                                             queues,
                                             local_logdir=local_logdir,
                                             background=(input_mode == InputMode.SPARK)))
    except Exception as e:
      logging.error("Exception in TF background thread")
      status['error'] = str(e)
      exception_handler()

  t = threading.Thread(target=_start, args=(tf_status,))
  # run as daemon thread so that in spark mode main thread can exit
  # if feeder spark stage fails and main thread can't do explicit shutdown
  t.daemon = True

  t.start()


  # wait for executors to check GPU presence
  logging.info("Waiting for GPU presence check to start")
  gpus_present = server.await_gpu_check()
  logging.info("All GPU checks completed")


  # wait for executors to register and start TFNodes before continuing
  logging.info("Waiting for TFSparkNodes to start")
  cluster_info = server.await_reservations(sc, tf_status, reservation_timeout)
  logging.info("All TFSparkNodes started")


  # print cluster_info and extract TensorBoard URL
  tb_url = None
  for node in cluster_info:
    logging.info(node)
    if node['tb_port'] != 0:
      tb_url = "http://{0}:{1}".format(node['host'], node['tb_port'])

  if tb_url is not None:
    logging.info("========================================================================================")
    logging.info("")
    logging.info("TensorBoard running at:       {0}".format(tb_url))
    logging.info("")
    logging.info("========================================================================================")

  # since our "primary key" for each executor's TFManager is (host, executor_id), sanity check for duplicates

  # Note: this may occur if Spark retries failed Python tasks on the same executor.
  tb_nodes = set()
  for node in cluster_info:
    node_id = (node['host'], node['executor_id'])
    if node_id in tb_nodes:
      raise Exception("Duplicate cluster node id detected (host={0}, executor_id={1})".format(node_id[0], node_id[1]) +
                      "Please ensure that:\n" +
                      "1. Number of executors >= number of TensorFlow nodes\n" +
                      "2. Number of tasks per executors is 1\n" +
                      "3, TFCluster.shutdown() is successfully invoked when done.")
    else:
      tb_nodes.add(node_id)

  # create TFCluster object
  cluster = TFCluster()
  cluster.sc = sc
  cluster.meta = cluster_meta
  cluster.nodeRDD = nodeRDD
  cluster.cluster_info = cluster_info
  cluster.cluster_meta = cluster_meta
  cluster.input_mode = input_mode
  cluster.queues = queues
  cluster.server = server

  return cluster


def exception_handler():
  global experiment_json
  global elastic_id
  if running and experiment_json != None:
    experiment_json = json.loads(experiment_json)
    experiment_json['status'] = "FAILED"
    experiment_json['finished'] = datetime.now().isoformat()
    experiment_json = json.dumps(experiment_json)
    util.put_elastic(hopshdfs.project_name(), app_id, str('dist' + str(elastic_id)), experiment_json)

def exit_handler():
  global experiment_json
  global elastic_id
  if running and experiment_json != None:
    experiment_json = json.loads(experiment_json)
    experiment_json['status'] = "KILLED"
    experiment_json['finished'] = datetime.now().isoformat()
    experiment_json = json.dumps(experiment_json)
    util.put_elastic(hopshdfs.project_name(), app_id, str('dist' + str(elastic_id)), experiment_json)

atexit.register(exit_handler)
