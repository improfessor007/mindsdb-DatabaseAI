import mysql.connector
from contextlib import closing

from mindsdb.integrations.libs.base_integration import BaseIntegration

class MySQLHandler(BaseIntegration):

    def __init__(self, config, name, **kwargs):
        super().__init__(config, name)
        self.connection = None
        self.mysql_url = None

        self.host = kwargs.get('host')
        self.port = kwargs.get('port')
        self.user = kwargs.get('user')
        self.password = kwargs.get('password')
        self.ssl = kwargs.get('ssl')
        self.ssl_ca = kwargs.get('ssl_ca')
        self.ssl_cert = kwargs.get('ssl_cert')
        self.ssl_key = kwargs.get('ssl_key')

    def connect(self):
        config = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password
        }
        if self.ssl is True:
            config['client_flags'] = [mysql.connector.constants.ClientFlag.SSL]
            if self.ssl_ca is not None:
                config["ssl_ca"] = self.ssl_ca
            if self.ssl_cert is not None:
                config["ssl_cert"] = self.ssl_cert
            if self.ssl_key is not None:
                config["ssl_key"] = self.ssl_key

        con = mysql.connector.connect(**config)
        self._setup()
        return con

    def _get_connect_string(self, table):
        user = f"{self.config['api']['mysql']['user']}_{self.name}"
        password = self.config['api']['mysql']['password']
        host = self.config['api']['mysql']['host']
        port = self.config['api']['mysql']['port']

        if password is None or password == '':
            connect = f'mysql://{user}@{host}:{port}/mindsdb/{table}'
        else:
            connect = f'mysql://{user}:{password}@{host}:{port}/mindsdb/{table}'

        return connect

    def _setup(self):
        self.run_native_query(f'DROP DATABASE IF EXISTS {self.mindsdb_database}')
        self.run_native_query(f'CREATE DATABASE IF NOT EXISTS {self.mindsdb_database}')

        connect = self._get_connect_string('predictors')

        q = f"""
            CREATE TABLE IF NOT EXISTS {self.mindsdb_database}.predictors (
                name VARCHAR(500),
                status VARCHAR(500),
                accuracy VARCHAR(500),
                predict VARCHAR(500),
                update_status VARCHAR(500),
                mindsdb_version VARCHAR(500),
                error VARCHAR(500),
                select_datarun_native_query VARCHAR(500),
                training_options VARCHAR(500),
                key name_key (name)
            ) ENGINE=FEDERATED CHARSET=utf8 CONNECTION='{connect}';
        """
        self.run_native_query(q)

        connect = self._get_connect_string('commands')

        q = f"""
            CREATE TABLE IF NOT EXISTS {self.mindsdb_database}.commands (
                command VARCHAR(500),
                key command_key (command)
            ) ENGINE=FEDERATED CHARSET=utf8 CONNECTION='{connect}';
        """
        self.run_native_query(q)

    def check_status(self):
        try:
            con = self.connect()
            with closing(con) as con:
                connected = con.is_connected()
        except Exception:
            connected = False
        return connected

    def run_native_query(self, statement, query, session):
        con = self.connect()
        with closing(con) as con:
            cur = con.cursor(dictionary=True, buffered=True)
            cur.execute(query)
            res = True
            try:
                res = cur.fetchall()
            except Exception:
                pass
            con.commit()
        return res

    def get_tables(self):
        q = "SHOW TABLES;"
        result = self.run_native_query(q)
        return result

    def get_views(self):
        q = "SHOW VIEWS;"
        result = self.run_native_query(q)
        return result

    def _register_predictors(self, model_data_arr):
        for model_meta in model_data_arr:
            name = model_meta['name']
            predict = model_meta['predict']
            if not isinstance(predict, list):
                predict = [predict]
            columns_sql = ','.join(self._to_mysql_table(
                model_meta['dtype_dict'],
                predict,
                list(model_meta['dtype_dict'].keys())
            ))
            columns_sql += ',`when_data` varchar(500)'
            columns_sql += ',`select_data_query` varchar(500)'
            for col in predict:
                columns_sql += f',`{col}_confidence` double'
                if model_meta['dtype_dict'][col] in (dtype.integer, dtype.float):
                    columns_sql += f',`{col}_min` double'
                    columns_sql += f',`{col}_max` double'
                columns_sql += f',`{col}_explain` varchar(500)'

            connect = self._get_connect_string(name)

            self._unregister_predictor(name)
            q = f"""
                CREATE TABLE {self.mindsdb_database}.{self._escape_table_name(name)} (
                    {columns_sql},
                    index when_data_index (when_data),
                    index select_data_query_index (select_data_query)
                ) ENGINE=FEDERATED CHARSET=utf8 CONNECTION='{connect}';
            """
            self.run_native_query(q)

    def _to_mysql_table(self, dtype_dict, predicted_cols, columns):
        subtype_map = {
            dtype.integer: 'int',
            dtype.float: 'double',
            dtype.binary: 'bool',
            dtype.date: 'Date',
            dtype.datetime: 'Datetime',
            dtype.binary: 'VARCHAR(500)',
            dtype.categorical: 'VARCHAR(500)',
            dtype.tags: 'VARCHAR(500)',
            dtype.image: 'VARCHAR(500)',
            dtype.video: 'VARCHAR(500)',
            dtype.audio: 'VARCHAR(500)',
            dtype.short_text: 'VARCHAR(500)',
            dtype.rich_text: 'VARCHAR(500)',
            dtype.quantity: 'VARCHAR(500)',
            dtype.num_array: 'VARCHAR(500)',
            dtype.cat_array: 'VARCHAR(500)',
            dtype.num_tsarray: 'VARCHAR(500)',
            dtype.cat_tsarray: 'VARCHAR(500)',
            'default': 'VARCHAR(500)'
        }

        column_declaration = []
        for name in columns:
            try:
                col_subtype = dtype_dict[name]
                new_type = subtype_map.get(col_subtype, subtype_map.get('default'))
                column_declaration.append(f' `{name}` {new_type} ')
                if name in predicted_cols:
                    column_declaration.append(f' `{name}_original` {new_type} ')
            except Exception as e:
                log.error(f'Error: can not determine mysql data type for column {name}: {e}')

        return column_declaration

    def _escape_table_name(self, name):
        return '`' + name.replace('`', '``') + '`'

    def _unregister_predictor(self, name):
        q = f"""
            drop table if exists {self.mindsdb_database}.{self._escape_table_name(name)};
        """
        self.run_native_query(q)

    def _get_row_count(self, query):
        q = f"""
            SELECT COUNT(*) as count
            FROM ({query}) as query;
        """
        result = self.run_native_query(q)
        return result[0]['count']

    def _get_columns(self, query):
        q = f"SELECT * from ({query}) LIMIT 1;"
        query_response = self.run_native_query(q)
        if len(query_response) > 0:
            columns = list(query_response[0].keys())
            return columns
        else:
            return []

    def select_query(self, statement, query, session):
        """ Here you can inter-operate betweens integrations. """
        if 'PREDICT' in query:  # <- idea
            for column in select:
                predict(column, df)  # multiple outputs for multiple predictors
        pass

    def join(self, left_integration_instance, left_where, on=None):
        # Can join either:
        #   - another DS
        #   - an ML model
        if not on:
            on = '*'
        pass

    def select_into(self, integration_instance, stmt):
        pass

    def describe_table(self, table_name):
        """ For getting standard info about a table. e.g. data types """
        q = f"DESCRIBE {table_name};"
        result = self.run_native_query(q)
        return result
