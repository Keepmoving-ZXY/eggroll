#
#  Copyright 2019 The FATE Authors. All Rights Reserved.
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
#

from typing import Iterable

#from arch.api.table import eggroll_util
#from eggroll.api import StoreType
# noinspection PyProtectedMember
from arch.api.table.eggroll2.table_impl import DTable
from arch.api.table.session import FateSession as TableManger
from eggroll.roll_pair.roll_pair import RollPairContext
from eggroll.core.constants import StoreTypes as ErStoreType


# noinspection PyProtectedMember
class FateSessionImpl(TableManger):
    """
    manage DTable, use EggRoleStorage as storage
    """

    def __init__(self, eggroll_session, work_mode, persistent_engine=ErStoreType.ROLLPAIR_LMDB):
        #self._eggroll = eggroll_util.build_eggroll_runtime(work_mode=work_mode, eggroll_session=eggroll_session)
        from eggroll.roll_pair.roll_pair import runtime_init
        self._eggroll = runtime_init(session=eggroll_session)
        self._session_id = eggroll_session.get_session_id()
        self._persistent_engine = persistent_engine
        self._session = eggroll_session
        TableManger.set_instance(self)

    def get_persistent_engine(self):
        return self._persistent_engine

    def table(self,
              name,
              namespace,
              partition,
              persistent,
              in_place_computing,
              create_if_missing,
              error_if_exist):
        # dtable = self._eggroll.table(name=name, namespace=namespace, partition=partition,
        #                              persistent=persistent, in_place_computing=in_place_computing,
        #                              create_if_missing=create_if_missing, error_if_exist=error_if_exist)
        options = dict()
        options["create_if_missing"] = create_if_missing
        options["total_partitions"] = partition
        dtable = self._eggroll.load(namespace=namespace, name=name, options=options)
        return DTable(dtable=dtable, session_id=self._session_id)

    def parallelize(self,
                    data: Iterable,
                    include_key,
                    name,
                    partition,
                    namespace,
                    persistent,
                    chunk_size,
                    in_place_computing,
                    create_if_missing,
                    error_if_exist):
        # dtable = self._eggroll.parallelize(data=data,
        #                                    include_key=include_key,
        #                                    name=name,
        #                                    partition=partition,
        #                                    namespace=namespace,
        #                                    persistent=persistent,
        #                                    chunk_size=chunk_size,
        #                                    in_place_computing=in_place_computing,
        #                                    create_if_missing=create_if_missing,
        #                                    error_if_exist=error_if_exist)
        options = dict()
        options["name"] = name
        options["namespace"] = namespace
        options["create_if_missing"] = create_if_missing
        options["total_partitions"] = partition
        
        options["include_key"] = include_key
        dtable = self._eggroll.parallelize(data=data, options=options)
        rdd_inst = DTable(dtable, session_id=self._session_id)

        return rdd_inst

    def cleanup(self, name, namespace, persistent):
        self._eggroll.cleanup(name=name, namespace=namespace, persistent=persistent)

    def generateUniqueId(self):
        return self._eggroll.generateUniqueId()

    def get_session_id(self):
        return self._session_id

    def stop(self):
        return self._session.stop()
        #self._eggroll.stop()
