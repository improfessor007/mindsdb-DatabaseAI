import os
import sqlite3
import requests

from ast import literal_eval
from typing import List, Union, Optional
from datetime import datetime

from mindsdb.integrations.libs.base_handler import PredictiveHandler
from mindsdb.utilities.config import Config
from mindsdb import __version__ as mindsdb_version
from mindsdb.utilities.functions import mark_process
from lightwood.api.types import ProblemDefinition
import mindsdb.interfaces.storage.db as db
from mindsdb.interfaces.model.model_controller import ModelController
from mindsdb_sql import parse_sql
from mindsdb_sql.parser.dialects.mindsdb import (
    CreateDatasource,
    RetrainPredictor,
    CreatePredictor,
    DropDatasource,
    DropPredictor,
    CreateView
)

import mlflow
from mlflow.tracking import MlflowClient
import pandas as pd


class MLflowIntegration(PredictiveHandler):
    def __init__(self, name):
        """
        An MLflow integration needs to have a working connection to work. For this:
            - All models to use should be previously served
            - An mlflow server should be running, to access the model registry
            
        Example:
            1. Run `mlflow server -p 5001 --backend-store-uri sqlite:///mlflow.db --default-artifact-root ./artifacts --host 0.0.0.0`
            2. Run `mlflow models serve --model-uri ./model_path`
            3. Instance this integration and call the `connect method` passing the relevant urls to mlflow and to the DB
            
        Note: above, `artifacts` is a folder to store artifacts for new experiments that do not specify an artifact store.
        """  # noqa
        super().__init__(name)
        self.mlflow_url = None
        self.registry_path = None
        self.connection = None
        self.parser = parse_sql
        self.dialect = 'mindsdb'
        mdb_config = Config()
        db_path = mdb_config['paths']['root']
        # TODO: sqlite path -> handler key-value store to read/write metadata given some context (e.g. user name) -> Max has an interface WIP
        # self.handler = PermissionHandler(user)  # to be implemented for model governance
        # registry_path = self.handler['integrations']['mlflow']['registry_path']
        # self.internal_registry = sqlite3.connect(registry_path)

        # this logic will not be used, too simple
        self.internal_registry = sqlite3.connect('models.db') # sqlite3.connect(os.path.join(db_path, 'models.db'))
        self._prepare_registry()

    def connect(self, mlflow_url, model_registry_path):
        """ Connect to the mlflow process using MlflowClient class. """  # noqa
        self.mlflow_url =  mlflow_url
        self.registry_path = model_registry_path
        self.connection = MlflowClient(self.mlflow_url, self.registry_path)
        return self.check_status()

    def _prepare_registry(self):
        """ Checks that sqlite records of registered models exists, otherwise creates it. """  # noqa
        cur = self.internal_registry.cursor()
        if ('models',) not in list(cur.execute("SELECT name FROM sqlite_master WHERE type='table';")):
            cur.execute("""create table models (model_name text, format text, target text, url text)""")  # TODO: dtype_dict?
        self.internal_registry.commit()

    def check_status(self):
        """ Checks that the connection is, as expected, an MlflowClient instance. """  # noqa
        # TODO: use a heartbeat method (pending answer in slack, potentially not possible)
        try:
            assert isinstance(self.connection, mlflow.tracking.MlflowClient)
        except AssertionError as e:
            return {'status': '503', 'error': e}  # service unavailable
        return {'status': '200'}  # ok

    def get_tables(self):
        """ Returns list of model names (that have been succesfully linked with CREATE PREDICTOR) """  # noqa
        cur = self.internal_registry.cursor()
        tables = [row[0] for row in list(cur.execute("SELECT model_name FROM models;"))]
        return tables

    def describe_table(self, table_name: str):
        """ For getting standard info about a table. e.g. data types """  # noqa
        model = None

        if table_name not in self.get_tables():
            raise Exception("Table not found.")

        models = {model.name: model for model in self.connection.list_registered_models()}
        model = models[table_name]
        latest_version = model.latest_versions[-1]
        description = {
            'NAME': model.name,
            'USER_DESCRIPTION': model.description,
            'LAST_STATUS': latest_version.status,
            'CREATED_AT': datetime.fromtimestamp(model.creation_timestamp//1000).strftime("%m/%d/%Y, %H:%M:%S"),
            'LAST_UPDATED': datetime.fromtimestamp(model.last_updated_timestamp//1000).strftime("%m/%d/%Y, %H:%M:%S"),
            'TAGS': model.tags,
            'LAST_RUN_ID': latest_version.run_id,
            'LAST_SOURCE_PATH': latest_version.source,
            'LAST_USER_ID': latest_version.user_id,
            'LAST_VERSION': latest_version.version,
        }
        return description

    def run_native_query(self, query_str: str):
        """ 
        Inside this method, anything is valid because you assume no inter-operability with other integrations.
        
        Currently supported:
            1. Create predictor: this will link a pre-existing (i.e. trained) mlflow model to a mindsdb table.
                To query the predictor, make sure you serve it first.
            2. Drop predictor: this will un-link a model that has been registered with the `create` syntax, meaning it will no longer be accesible as a table.
                
        :param query: raw query string
        :param statement: query as parsed and interpreted as a SQL statement by the mindsdb parser 
        :param session: mindsdb session that contains the model interface and data store, among others
         
        """  # noqa
        # TODO / Notes
        # all I/O should be handled by the integration. mdb (at higher levels) should not concern itself with how
        # anything is stored, just providing the context to company/users
            # e.g. lightwood: save/load models, store metadata about models, all that is delegated to mdb.

        # todo
        statement = self.parser(query_str, dialect=self.dialect)  # one of mindsdb_sql:mindsdb dialect types

        if type(statement) == CreatePredictor:
            model_name = statement.name.parts[-1]

            # check that it exists within mlflow and is not already registered
            mlflow_models = [model.name for model in self.connection.list_registered_models()]
            if model_name not in mlflow_models:
                print("Error: this predictor is not registered in mlflow. Check you are serving it and try again.")
            elif model_name in self.get_tables():
                # @TODO: maybe add re-wiring so that a predictor name can point to a new endpoint?
                # i.e. add _edit_invocation_url method that edits the db.record and self.internal_registry
                print("Error: this model is already registered!")
            else:
                target = statement.targets[0].parts[-1]  # TODO: multiple target support?
                pdef = {
                    'format': statement.using['format'],
                    'dtype_dict': statement.using['dtype_dict'],
                    'target': target,
                    'url': statement.using['url.predict']
                }
                cur = self.internal_registry.cursor()
                cur.execute("insert into models values (?,?,?,?)",
                            (model_name, pdef['format'], pdef['target'], pdef['url']))
                self.internal_registry.commit()

        elif type(statement) == DropPredictor:
            predictor_name = statement.name.parts[-1]
            session.datahub['mindsdb'].delete_predictor(predictor_name)
            cur = self.internal_registry.cursor()
            cur.execute("""DELETE FROM models WHERE model_name='{predictor_name}'""")
            self.internal_registry.commit()

        else:
            raise Exception(f"Query type {type(statement)} not supported")

    def select_query(self, stmt):
        """
        This assumes the raw_query has been parsed with mindsdb_sql and so the stmt has all information we need.
        In general, for this method in all subclasses you can inter-operate betweens integrations here.
        """  # noqa
        model_name = stmt.from_table.parts[-1]

        mlflow_models = [model.name for model in self.connection.list_registered_models()]
        if not model_name in self.get_tables():
            raise Exception("Error, not found. Please create this predictor first.")
        elif not model_name in mlflow_models:
            raise Exception(
                "Cannot connect with the model, it might not served. Please serve it with MLflow and try again.")

        cur = self.internal_registry.cursor()
        _, _, target, model_url = list(cur.execute(f'select * from models where model_name="{model_name}";'))[0]
        model = self.connection.get_registered_model(model_name)

        if target not in [str(t) for t in parsed.targets]:
            raise Exception("Predictor will not be called, target column is not specified.")

        df = pd.DataFrame.from_dict({stmt.where.args[0].parts[0]: [stmt.where.args[1].value]})
        resp = requests.post(model_url,
                             data=df.to_json(orient='records'),
                             headers={'content-type': 'application/json; format=pandas-records'})
        answer: List[object] = resp.json()

        predictions = pd.DataFrame({'prediction': answer})
        out = df.join(predictions)
        return out

    def join(self, left_integration_instance, left_where, on=None):
        """
        Within this method one should specify specialized logic particular to how the framework would pre-process
        data coming from a datasource integration, before actually passing the selected dataset to the model.
        """  # noqa
        # TODO
        if not on:
            on = '*'
        pass


if __name__ == '__main__':
    # TODO: turn this into tests

    registered_model_name = 'nlp_kaggle3'  # already saved to mlflow local instance
    cls = MLflowIntegration('test_mlflow')
    print(cls.connect(
        mlflow_url='http://127.0.0.1:5001',  # for this test, serve at 5001 and served model at 5000
        model_registry_path='sqlite:////Users/Pato/Work/MindsDB/temp/experiments/BYOM/mlflow.db'))
    try:
        cls.run_native_query(f"DROP PREDICTOR {registered_model_name}")
    except:
        pass
    query = f"CREATE PREDICTOR {registered_model_name} PREDICT target USING url.predict='http://localhost:5000/invocations', format='mlflow', dtype_dict={{'text': 'rich_text', 'target': 'binary'}}"
    cls.run_native_query(query)
    print(cls.get_tables())
    print(cls.describe_table(f'{registered_model_name}'))

    # Tests with MySQL handler: JOIN
    from mindsdb.integrations.mysql_handler.mysql_handler.mysql_handler import MySQLHandler  # expose through parent init
    kwargs = {
        "host": "localhost",
        "port": "3306",
        "user": "root",
        "password": "root",
        "database": "test",
        "ssl": False
    }
    handler = MySQLHandler('test_handler', **kwargs)
    assert handler.check_status()

    query = f"SELECT target from {registered_model_name} WHERE text='This is nice.'"
    parsed = cls.parser(query, dialect=cls.dialect)
    predicted = cls.select_query(parsed)

