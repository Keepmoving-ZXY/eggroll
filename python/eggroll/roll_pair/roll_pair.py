# -*- coding: utf-8 -*-
#  Copyright (c) 2019 - now, Eggroll Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
from typing import Iterable

import grpc
from grpc._cython import cygrpc

from eggroll.core.command.command_model import ErCommandRequest, ErCommandResponse, CommandURI
from eggroll.core.datastructure.broker import FifoBroker
from eggroll.core.io.format import BinBatchReader
from eggroll.core.meta_model import ErStoreLocator, ErJob, ErStore, ErFunctor, ErTask, ErEndpoint, ErPair, ErPartition, \
  ErProcessor
from eggroll.core.proto import command_pb2_grpc, transfer_pb2_grpc
from eggroll.core.serdes import cloudpickle, eggroll_serdes
from eggroll.core.command.command_client import CommandClient
from eggroll.core.client import ClusterManagerClient
from eggroll.core.grpc.factory import GrpcChannelFactory
from eggroll.core.constants import StoreTypes, SerdesTypes, PartitionerTypes, ProcessorTypes
from eggroll.core.serdes.eggroll_serdes import PickleSerdes, CloudPickleSerdes, EmptySerdes
from eggroll.core.session import ErSession
from eggroll.core.transfer.transfer_service import GrpcTransferServicer, TransferClient
from eggroll.core.utils import string_to_bytes, hash_code
from eggroll.roll_pair.egg_pair import EggPair
from eggroll.roll_pair.shuffler import DefaultShuffler
from concurrent import futures
from eggroll.core.io.kv_adapter import LmdbSortedKvAdapter, RocksdbSortedKvAdapter
from eggroll.core.io.io_utils import get_db_path

class RollPairContext(object):

    def __init__(self, session: ErSession):
        self.__session = session
        self.session_id = session.get_session_id()
        # self.default_store_type = StoreTypes.ROLLPAIR_LMDB
        self.default_store_type = StoreTypes.ROLLPAIR_LMDB

    def get_roll_endpoint(self):
        return self.__session._rolls[0]

    # TODO: return transfer endpoint
    def get_egg_endpoint(self, egg_id=0):
        return self.__session._eggs[0][0]._command_endpoint

    def load(self, namespace=None, name=None, create_if_missing=True, options={}):
        store_type = options.get('store_type', self.default_store_type)
        total_partitions = options.get('total_partitions', 1)
        partitioner = options.get('partitioner', PartitionerTypes.BYTESTRING_HASH)
        serdes = options.get('serdes', SerdesTypes.CLOUD_PICKLE)
        store = ErStore(
            store_locator=ErStoreLocator(
                store_type=store_type,
                namespace=namespace,
                name=name,
                total_partitions=total_partitions,
                partitioner=partitioner,
                serdes=serdes))

        result = self.__session.cm_client.get_or_create_store(store)
        return RollPair(result, self)

def default_partitioner(k):
    return 0
def default_egg_router(k):
    return 0

class RollPair(object):
  __uri_prefix = 'v1/roll-pair'
  GET = "get"
  PUT = "put"
  GET_ALL = "getAll"
  PUT_ALL = "put_all"
  MAP = 'map'
  MAP_VALUES = 'mapValues'
  REDUCE = 'reduce'
  JOIN = 'join'
  AGGREGATE = 'aggregate'
  COLLAPSEPARTITIONS = 'collapsePartitions'
  MAPPARTITIONS = 'mapPartitions'
  GLOM = 'glom'
  FLATMAP = 'flatMap'
  SAMPLE = 'sample'
  FILTER = 'filter'
  SUBTRACTBYKEY = 'subtractByKey'
  UNION = 'union'
  RUNJOB = 'runJob'

  def __partitioner(self):
    def partitioner_wrapper(hash_func, total_partitions):
      def hash_mod(k):
        return hash_func(k) % total_partitions
      return hash_mod
    return partitioner_wrapper

  def __init__(self, er_store: ErStore, rp_ctx: RollPairContext):
    self.__command_serdes =  SerdesTypes.PROTOBUF
    self.__roll_pair_command_client = CommandClient()
    self.__store = er_store
    self.value_serdes = self.get_serdes()
    self.key_sedes = self.get_serdes()
    self.partitioner = self.__partitioner()(hash_code, self.__store._store_locator._total_partitions)
    self.egg_router = default_egg_router
    self.ctx = rp_ctx
    self.__session_id = self.ctx.session_id
    #TODO: config or auto
    self._transfer_server_endpoint = "localhost:60668"


  def __repr__(self):
    return f'python RollPair(_store={self.__store})'

  def get_serdes(self):
    serdes_type = self.__store._store_locator._serdes
    print(f'serdes type: {serdes_type}')
    if serdes_type == SerdesTypes.CLOUD_PICKLE or serdes_type == SerdesTypes.PROTOBUF:
      return CloudPickleSerdes
    elif serdes_type == SerdesTypes.PICKLE:
      return PickleSerdes
    else:
      return EmptySerdes

  def kv_to_bytes(self, **kwargs):
    use_serialize = kwargs.get("use_serialize", True)
    # can not use is None
    if "k" in kwargs and "v" in kwargs:
      k, v = kwargs["k"], kwargs["v"]
      return (self.value_serdes.serialize(k), self.value_serdes.serialize(v)) if use_serialize \
        else (string_to_bytes(k), string_to_bytes(v))
    elif "k" in kwargs:
      k = kwargs["k"]
      return self.value_serdes.serialize(k) if use_serialize else string_to_bytes(k)
    elif "v" in kwargs:
      v = kwargs["v"]
      return self.value_serdes.serialize(v) if use_serialize else string_to_bytes(v)

  """
  
    storage api
  
  """
  def get(self, k, opt={}):
    k = self.get_serdes().serialize(k)
    er_pair = ErPair(key=k, value=None)
    outputs = []
    value = None
    part_id = self.partitioner(k)
    egg_id = self.egg_router(part_id)
    egg_endpoint = self.ctx.get_egg_endpoint(egg_id)
    print(egg_endpoint)
    print("count:", self.__store._store_locator._total_partitions)
    inputs = [ErPartition(id=part_id, store_locator=self.__store._store_locator)]
    output = [ErPartition(id=part_id, store_locator=self.__store._store_locator)]

    job = ErJob(id=self.__session_id, name=RollPair.GET,
              inputs=[self.__store],
              outputs=outputs,
              functors=[ErFunctor(body=cloudpickle.dumps(er_pair))])
    task = ErTask(id=self.__session_id, name=RollPair.GET, inputs=inputs, outputs=output, job=job)
    print("start send req")
    job_resp = self.__roll_pair_command_client.simple_sync_send(
              input=task,
              output_type=ErPair,
              endpoint=egg_endpoint,
              command_uri=CommandURI(f'{EggPair.uri_prefix}/{EggPair.GET}'),
              serdes_type=self.__command_serdes
    )
    print("get resp:{}".format(ErPair.from_proto_string(job_resp._value)))

    value = self.value_serdes.deserialize(job_resp._value)

    return value

  def __get_all_standalone(self, opt={}):
    path = get_db_path(self.__store._partitions)
    input_adapter = LmdbSortedKvAdapter(path)
    input_iterator = input_adapter.iteritems()
    for k, v in input_iterator:
      yield self.get_serdes().deserialize(k), self.get_serdes().deserialize(v)

  def __put_all_standalone(self, items, output=None,opt={}):
    is_include_key = opt.get("is_include_key")
    path = get_db_path(self.__store._partitions)
    input_adapter = LmdbSortedKvAdapter(path)
    input_iterator = input_adapter.iteritems()
    if isinstance(items, Iterable):
      for k, v in items:
        input_adapter.put(self.value_serdes.serialize(k), self.value_serdes.serialize(v))
    else:
      raise EnvironmentError("iterable obj is required")

  def get_all(self, opt = {}):
    return self.__get_all_standalone()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=1),
                         options=[
                           (cygrpc.ChannelArgKey.max_send_message_length, -1),
                           (cygrpc.ChannelArgKey.max_receive_message_length, -1)])
    transfer_servicer = GrpcTransferServicer()
    transfer_pb2_grpc.add_TransferServiceServicer_to_server(transfer_servicer,
                                                            server)

    port = server.add_insecure_port(f'[::]:{0}')
    print("starting roll pair recv service, port:{}".format(port))
    server.start()

    functor = ErFunctor(name=EggPair.GET_ALL, serdes=SerdesTypes.CLOUD_PICKLE,
                        body=cloudpickle.dumps(ErEndpoint(host="localhost", port=port)))
    outputs = []
    print("session id:{}".format(self.__session_id))
    job = ErJob(id=self.__session_id, name=EggPair.GET_ALL,
                inputs=[self.__store],
                outputs=outputs,
                functors=[functor])
    task = ErTask(id=self.__session_id, name=RollPair.GET_ALL, inputs=self.__store._partitions, outputs=[], job=job)
      #TODO: send to all eggs
    print("get egg port:{}".format(self.ctx.get_egg_endpoint()._port))
    print("job id:{}".format(job._id))
    job_result = self.__roll_pair_command_client.simple_sync_send(
        input=task,
        output_type=ErTask,
        endpoint=self.ctx.get_egg_endpoint(),
        command_uri=CommandURI(f'{EggPair.uri_prefix}/{EggPair.GET_ALL}'),
        serdes_type=self.__command_serdes)

    broker = GrpcTransferServicer.get_broker(str(job._id))
    while not broker.is_closable():
      proto_transfer_batch = broker.get(block=True, timeout=1)
      if proto_transfer_batch:
          bin_data = proto_transfer_batch.data
      reader = BinBatchReader(pair_batch=bin_data)
      try:
          while reader.has_remaining():
            key_size = reader.read_int()
            key = reader.read_bytes(size=key_size)
            value_size = reader.read_int()
            value = reader.read_bytes(size=value_size)
            yield key, value
      except IndexError as e:
          print('finish processing an output')
    GrpcTransferServicer.remove_broker(job._id)

  def put_all(self, items, output=None,opt = {}):
      return self.__put_all_standalone(items)
      outputs = []
      if output:
        outputs.append(output)

      job = ErJob(id=self.__session_id, name="putAll",
                  inputs=[self.__store],
                  outputs=outputs,
                  functors=[])
      task = ErTask(id=self.__session_id, name=RollPair.PUT, inputs=inputs, outputs=output, job=job)
      print("start send req")
      job_result = self.__roll_pair_command_client.simple_sync_send(
          input=job,
          output_type=ErJob,
          endpoint=self.__roll_pair_service_endpoint,
          command_uri=CommandURI(f'{RollPair.__uri_prefix}/{RollPair.MAP_VALUES}'),
          serdes_type=self.__command_serdes)
      # TODO: all aggs ?
      shuffle_broker = FifoBroker()
      shuffler = DefaultShuffler(task._job._id, shuffle_broker, output_store, output_partition, p)

      for k1, v1 in items:
        shuffle_broker.put(self.serde.serialize(k1), self.serde.serialize(v1))
      print('finish calculating')
      shuffle_broker.signal_write_finish()
      # TODO: async ?
      shuffler.start()

      shuffle_finished = shuffler.wait_until_finished(600)
      return None
  def put(self, k, v, opt = {}):
    k, v = self.get_serdes().serialize(k), self.get_serdes().serialize(v)
    er_pair = ErPair(key=k, value=v)
    outputs = []
    part_id = self.partitioner(k)
    egg_id = self.egg_router(part_id)
    egg_endpoint = self.ctx.get_egg_endpoint(egg_id)
    inputs = [ErPartition(id=part_id, store_locator=self.__store._store_locator)]
    output = [ErPartition(id=0, store_locator=self.__store._store_locator)]

    job = ErJob(id=self.__session_id, name=RollPair.PUT,
                inputs=[self.__store],
                outputs=outputs,
                functors=[ErFunctor(body=cloudpickle.dumps(er_pair))])
    task = ErTask(id=self.__session_id, name=RollPair.PUT, inputs=inputs, outputs=output, job=job)
    print("start send req")
    job_resp = self.__roll_pair_command_client.simple_sync_send(
      input=task,
      output_type=ErPair,
      endpoint=egg_endpoint,
      command_uri=CommandURI(f'{EggPair.uri_prefix}/{EggPair.PUT}'),
      serdes_type=self.__command_serdes
    )
    print("get resp:{}".format(ErPair.from_proto_string(job_resp._value)))
    value = job_resp._value
    return value

  # computing api
  def map_values(self, func, output = None, options = {}):
    functor = ErFunctor(name=RollPair.MAP_VALUES, serdes=SerdesTypes.CLOUD_PICKLE, body=cloudpickle.dumps(func))
    outputs = []
    if output:
      outputs.append(output)
    job = ErJob(id=self.__session_id, name=RollPair.MAP_VALUES,
                inputs=[self.__store],
                outputs=outputs,
                functors=[functor])

    job_result = self.__roll_pair_command_client.simple_sync_send(
        input=job,
        output_type=ErJob,
        endpoint=self.__roll_pair_service_endpoint,
        command_uri=CommandURI(f'{RollPair.__uri_prefix}/{RollPair.MAP_VALUES}'),
        serdes_type=self.__command_serdes)

    er_store = job_result._outputs[0]
    print(er_store)

    return RollPair(er_store, options=self._parent_opts)

  def map(self, func, output = None, options = {}):
    functor = ErFunctor(name=RollPair.MAP, serdes=SerdesTypes.CLOUD_PICKLE, body=cloudpickle.dumps(func))
    outputs = []
    if output:
      outputs.append(output)
    job = ErJob(id=self.__session_id, name=RollPair.MAP,
                inputs=[self.__store],
                outputs=outputs,
                functors=[functor])

    job_result = self.__roll_pair_command_client.simple_sync_send(
        input = job,
        output_type = ErJob,
        endpoint = self.__roll_pair_service_endpoint,
        command_uri = CommandURI(f'{RollPair.__uri_prefix}/{RollPair.MAP}'),
        serdes_type=self.__command_serdes)

    er_store = job_result._outputs[0]
    print(er_store)

    return RollPair(er_store, options=self._parent_opts)

  def map_partitions(self, func, output = None, options = {}):
    functor = ErFunctor(name=RollPair.MAPPARTITIONS, serdes=SerdesTypes.CLOUD_PICKLE, body=cloudpickle.dumps(func))
    outputs = []
    if output:
      outputs.append(output)
    job = ErJob(id=self.__session_id, name=RollPair.MAPPARTITIONS,
                 inputs=[self.__store],
                 outputs=outputs,
                 functors=[functor])

    job_result = self.__roll_pair_command_client.simple_sync_send(
      input=job,
      output_type=ErJob,
      endpoint=self.__roll_pair_service_endpoint,
      command_uri=CommandURI(f'{RollPair.__uri_prefix}/{RollPair.MAPPARTITIONS}'),
      serdes_type=self.__command_serdes
    )
    er_store = job_result._outputs[0]
    print(er_store)

    return RollPair(er_store, options=self._parent_opts)

  def collapse_partitions(self, func, output = None, opt = {}):
    functor = ErFunctor(name=RollPair.COLLAPSEPARTITIONS, serdes=SerdesTypes.CLOUD_PICKLE, body=cloudpickle.dumps(func))
    outputs = []
    if output:
      outputs.append(output)

    job = ErJob(id=self.__session_id, name=RollPair.COLLAPSEPARTITIONS,
                inputs=[self.__store],
                outputs=outputs,
                functors=[functor])

    job_result = self.__roll_pair_command_client.simple_sync_send(
      input=job,
      output_type=ErJob,
      endpoint=self.__roll_pair_service_endpoint,
      command_uri=CommandURI(f'{RollPair.__uri_prefix}/{RollPair.COLLAPSEPARTITIONS}'),
      serdes_type=self.__command_serdes
    )
    er_store = job_result._outputs[0]
    print(er_store)

    return RollPair(er_store, options=self._parent_opts)

  def flat_map(self, func, output=None, opt={}):
    functor = ErFunctor(name=RollPair.FLATMAP, serdes=SerdesTypes.CLOUD_PICKLE, body=cloudpickle.dumps(func))
    outputs = []
    if output:
      outputs.append(output)

    job = ErJob(id=self.__session_id, name=RollPair.FLATMAP,
                inputs=[self.__store],
                outputs=outputs,
                functors=[functor])

    job_result = self.__roll_pair_command_client.simple_sync_send(
      input=job,
      output_type=ErJob,
      endpoint=self.__roll_pair_service_endpoint,
      command_uri=CommandURI(f'{RollPair.__uri_prefix}/{RollPair.FLATMAP}'),
      serdes_type=self.__command_serdes
    )
    er_store = job_result._outputs[0]
    print(er_store)

    return RollPair(er_store, options=self._parent_opts)

  def reduce(self, func, output = None, options = {}):
    functor = ErFunctor(name=RollPair.REDUCE, serdes=SerdesTypes.CLOUD_PICKLE, body=cloudpickle.dumps(func))

    outputs = []
    if output:
      outputs.append(output)
    job = ErJob(id=self.__session_id, name=RollPair.REDUCE,
                inputs=[self.__store],
                outputs=outputs,
                functors=[functor])

    job_result = self.__roll_pair_command_client.simple_sync_send(
        input = job,
        output_type = ErJob,
        endpoint = self.__roll_pair_service_endpoint,
        command_uri = CommandURI(f'{RollPair.__uri_prefix}/{RollPair.REDUCE}'),
        serdes_type=self.__command_serdes)

    er_store = job_result._outputs[0]

    return RollPair(er_store, options=self._parent_opts)

  def aggregate(self, zero_value, seq_op, comb_op, output = None, options = {}):
    zero_value_functor = ErFunctor(name=RollPair.AGGREGATE, serdes=SerdesTypes.CLOUD_PICKLE, body=cloudpickle.dumps(zero_value))
    seq_op_functor = ErFunctor(name=RollPair.AGGREGATE, serdes=SerdesTypes.CLOUD_PICKLE, body=cloudpickle.dumps(seq_op))
    comb_op_functor = ErFunctor(name=RollPair.AGGREGATE, serdes=SerdesTypes.CLOUD_PICKLE, body=cloudpickle.dumps(comb_op))

    outputs = []
    if output:
      outputs.append(output)
    job = ErJob(id=self.__session_id, name=RollPair.AGGREGATE,
                inputs=[self.__store],
                outputs=outputs,
                functors=[zero_value_functor, seq_op_functor, comb_op_functor])

    job_result = self.__roll_pair_command_client.simple_sync_send(
        input = job,
        output_type = ErJob,
        endpoint = self.__roll_pair_service_endpoint,
        command_uri = CommandURI(f'{RollPair.__uri_prefix}/{RollPair.RUNJOB}'),
        serdes_type=self.__command_serdes)

    er_store = job_result._outputs[0]

    return RollPair(er_store, options=self._parent_opts)

  def glom(self, output=None, opt={}):
    functor = ErFunctor(name=RollPair.GLOM, serdes=SerdesTypes.CLOUD_PICKLE)
    outputs = []
    if output:
      outputs.append(output)

    job = ErJob(id=self.__session_id, name=RollPair.GLOM,
                inputs=[self.__store],
                outputs=outputs,
                functors=[functor])

    job_result = self.__roll_pair_command_client.simple_sync_send(
      input=job,
      output_type=ErJob,
      endpoint=self.__roll_pair_service_endpoint,
      command_uri=CommandURI(f'{RollPair.__uri_prefix}/{RollPair.GLOM}'),
      serdes_type=self.__command_serdes
    )
    er_store = job_result._outputs[0]
    print(er_store)

    return RollPair(er_store, options=self._parent_opts)

  def sample(self, fraction, seed=None, output=None, opt={}):
    er_fraction = ErFunctor(name=RollPair.REDUCE, serdes=SerdesTypes.CLOUD_PICKLE, body=cloudpickle.dumps(fraction))
    er_seed  = ErFunctor(name=RollPair.REDUCE, serdes=SerdesTypes.CLOUD_PICKLE, body=cloudpickle.dumps(seed))

    outputs = []
    if output:
      outputs.append(output)
    job = ErJob(id=self.__session_id, name=RollPair.SAMPLE,
                inputs=[self.__store],
                outputs=outputs,
                functors=[er_fraction, er_seed])

    job_result = self.__roll_pair_command_client.simple_sync_send(
      input=job,
      output_type=ErJob,
      endpoint=self.__roll_pair_service_endpoint,
      command_uri=CommandURI(f'{RollPair.__uri_prefix}/{RollPair.SAMPLE}'),
      serdes_type=self.__command_serdes)

    er_store = job_result._outputs[0]
    print(er_store)

    return RollPair(er_store, options=self._parent_opts)

  def filter(self, func, output=None, opt={}):
    functor = ErFunctor(name=RollPair.FILTER, serdes=SerdesTypes.CLOUD_PICKLE, body=cloudpickle.dumps(func))

    outputs = []
    if output:
      outputs.append(output)
    job = ErJob(id=self.__session_id, name=RollPair.FILTER,
                inputs=[self.__store],
                outputs=outputs,
                functors=[functor])

    job_result = self.__roll_pair_command_client.simple_sync_send(
      input=job,
      output_type=ErJob,
      endpoint=self.__roll_pair_service_endpoint,
      command_uri=CommandURI(f'{RollPair.__uri_prefix}/{RollPair.FILTER}'),
      serdes_type=self.__command_serdes)

    er_store = job_result._outputs[0]
    print(er_store)

    return RollPair(er_store, options=self._parent_opts)

  def subtract_by_key(self, other, output=None, opt={}):
    functor = ErFunctor(name=RollPair.SUBTRACTBYKEY, serdes=SerdesTypes.CLOUD_PICKLE)
    outputs = []
    if output:
      outputs.append(output)
    job = ErJob(id=self.__session_id, name=RollPair.SUBTRACTBYKEY,
                inputs=[self.__store, other.__store],
                outputs=outputs,
                functors=[functor])

    job_result = self.__roll_pair_command_client.simple_sync_send(
      input=job,
      output_type=ErJob,
      endpoint=self.__roll_pair_service_endpoint,
      command_uri=CommandURI(f'{RollPair.__uri_prefix}/{RollPair.SUBTRACTBYKEY}'),
      serdes_type=self.__command_serdes)
    er_store = job_result._outputs[0]
    print(er_store)

    return RollPair(er_store, options=self._parent_opts)

  def union(self, other, func=lambda v1, v2: v1, output=None, opt={}):
    functor = ErFunctor(name=RollPair.SUBTRACTBYKEY, serdes=SerdesTypes.CLOUD_PICKLE, body=cloudpickle.dumps(func))
    outputs = []
    if output:
      outputs.append(output)
    job = ErJob(id=self.__session_id, name=RollPair.SUBTRACTBYKEY,
                inputs=[self.__store, other.__store],
                outputs=outputs,
                functors=[functor])

    job_result = self.__roll_pair_command_client.simple_sync_send(
      input=job,
      output_type=ErJob,
      endpoint=self.__roll_pair_service_endpoint,
      command_uri=CommandURI(f'{RollPair.__uri_prefix}/{RollPair.SUBTRACTBYKEY}'),
      serdes_type=self.__command_serdes)
    er_store = job_result._outputs[0]
    print(er_store)

    return RollPair(er_store, options=self._parent_opts)

  def join(self, other, func, output=None, opt={}):
    functor = ErFunctor(name=RollPair.JOIN, serdes=SerdesTypes.CLOUD_PICKLE, body=cloudpickle.dumps(func))
    outputs = []
    if output:
      outputs.append(output)
    job = ErJob(id=self.__session_id, name=RollPair.JOIN,
                inputs=[self.__store, other.__store],
                outputs=outputs,
                functors=[functor])

    job_result = self.__roll_pair_command_client.simple_sync_send(
      input=job,
      output_type=ErJob,
      endpoint=self.__roll_pair_service_endpoint,
      command_uri=CommandURI(f'{RollPair.__uri_prefix}/{RollPair.JOIN}'),
      serdes_type=self.__command_serdes)
    er_store = job_result._outputs[0]
    print(er_store)

    return RollPair(er_store, options=self._parent_opts)
