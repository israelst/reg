from typing import List, Dict, Tuple, Any, Union
import dotenv
import os
import ollama
import loguru
import duckdb
import sqlalchemy
from sqlalchemy import create_engine, sql
from base_agent.llminterface import LangModel


dotenv.load_dotenv()
logger = loguru.logger


class Database:
    def __init__(self, dburl) -> None:
        """
        Configure database connection for prompt generation
        :param dburl: any standard database url or csv:mycsv.csv for csv files
        """
        self.url = dburl
        self._tables = []
        self._views = []
        self.table_descriptions = {}
        self.dialect = 'duckdb' if 'duckdb' in self.url else 'postgresql' if 'postgresql' in self.url else 'csv'

    @property
    def tables(self) -> List[str]:
        """
        Returns the list of tables in the database
        :return:
        """
        if not self._tables:
            if 'duckdb' in self.url:
                query = "SHOW TABLES;"
                result = self.connection.execute(query)
                self._tables = [row[0] for row in result.fetchall()]
                return self._tables
            elif 'postgresql' in self.url:
                query = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';"
            elif 'csv' in self.url:
                query = f"DESCRIBE TABLE '{self.url.split(':')[1]}';"
            result = self.connection.execute(sql.text(query))
            self._tables = [row[0] for row in result.fetchall()]
        return self._tables

    @property
    def views(self) -> List[str]:
        """
        Returns the list of views in the database
        :return:
        """
        if not self._views:
            if 'duckdb' in self.url:
                query = "SHOW VIEWS;"
            elif 'postgresql' in self.url:
                query = "SELECT table_name FROM information_schema.views WHERE table_schema = 'public';"
            result = self.connection.execute(sql.text(query))
            self._views = [row[0] for row in result.fetchall()]
        return self._views

    @property
    def connection(self) -> Union[duckdb.DuckDBPyConnection, sqlalchemy.engine.Connection]:
        if 'duckdb' in self.url:
            return get_duckdb_connection(self.url)
        elif 'postgresql' in self.url:
            engine = create_engine(self.url)
            return engine.connect()
        elif 'csv':
            mdb = duckdb.connect()
            # mdb.execute(f"select * from '{self.url.split(':')[1]}';")
            return mdb
        else:
            logger.error(f"Database URL {self.url} not supported.")

    def get_table_description(self, table_name: str) -> List[Dict[str, Any]]:
        """
        Returns the description of the table using duckdb
        :param table_name: name of the table
        :return:
        """
        if 'duckdb' in self.url:
            query = f"DESCRIBE SELECT * FROM {table_name};"
            result = self.connection.execute(query).fetchall()
        elif 'postgresql' in self.url:
            query = f"SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '{table_name}';"
            result = self.connection.execute(sql.text(query)).fetchall()
        elif 'csv' in self.url:
            result = get_csv_description(self.url.split(":")[-1])
            return result

        sample_rows = self._get_sample_rows(table_name)
        parsed_result = self._parse_table_description(result, sample_rows)
        self.table_descriptions[table_name] = parsed_result

        return parsed_result
    def _count_non_null_rows(self, table_name: str) -> List[int:]:
        pass

    def _get_sample_rows(self, table_name: str, num_rows: int = 5) -> List[Tuple[str, Any]]:
        """
        Returns a sample of rows from the table
        :param table_name: name of the table
        :param num_rows: number of rows to return
        :return: list of tuples
        """
        if 'duckdb' in self.url:
            query = f"SELECT * FROM {table_name} LIMIT {num_rows};"
            result = self.connection.execute(query).fetchall()
        elif 'postgresql' in self.url:
            query = f"SELECT * FROM {table_name} LIMIT {num_rows};"
            result = self.connection.execute(sql.text(query)) .fetchall()
        elif 'csv' in self.url:
            query = f"SELECT * FROM '{self.url.split(':')[1]}' LIMIT {num_rows};"
            return self.connection.execute(query).fetchall()

        return result

    def _parse_table_description(self, table_description: str, sample: List) -> str:
        """
        Parses the table description into a human readable format
        :param table_description: description as generated by the database
        :return: human readable table description
        """
        description = ""
        for n, column in enumerate(table_description):
            description += f"column name:{column[0]},  type:{column[1]}, sample values: {[r[n] for r in sample]}\n"
        return description
    def _create_semantic_view(self, table_name: str, view_name: str = None, duckdb_view: bool = False) -> None:
        """
        Creates a view in the database with semantic renaming of the columns for enhanced readability
        :param table_name: table name in the database to create the view on
        :param view_name: view name. if not given will default to table_name_semanticview
        :duckdb_view: if True, will create an in memory duckdb view instead of a sql view
        """
        # get current table description
        table_description = self.get_table_description(table_name)
        # Prompt gemma model through ollama to generate SQL code with semantic naming for the view
        view_name = view_name if view_name else f"{table_name}_semanticview"
        context = f"You will be asked to create SQL code in {self.dialect} dialect, to create a view with semantic "\
        f"names for all the columns of a table. Be mindful of including only existing columns, as listed in the context. Do not use uppercase letters or spaces in the semantic column names."\
        f"Don't use spaces, use underscores instead in variable names. Return pure and complete SQL clauses, which can be executed, without any accessory text."\
        f"When you cannot propose a semantic name, maintain the original name\n"
        prompt = f"Generate a view of table {table_name}, named {view_name}  "\
        "renaming column names with semantic names "\
        f"including the columns described bellow:\\n{table_description}"
        LM = LangModel(model='gpt')

        code = LM.get_response(question=prompt, context=context)
        result = self.run_query(code, table_name)
        # parse the response to get the column descriptions
        column_descriptions = {}
        for line in (result.split("\n")):
            if line.startswith("Column"):
                column_name = line.split(":")[1].strip()
            elif line.startswith("Description"):
                description = line.split(":")[1].strip()
                column_descriptions[column_name] = description

        logger.info(f"Created semantic view {view_name} on table {table_name}")
        logger.info(f"using the following SQL code:\n{code}")
        # print(column_descriptions)
        return column_descriptions

    def run_query(self, query: str, table_name: str, debug_tries: int = 5) -> List[Tuple[str, Any]]:
        """
        Run a query through the database connection, debugging it if necessary
        :param query: SQL query to run
        :param table_name: Table name to run the query on
        :param debug_tries: number of times to try debugging the query
        :return:
        """
        # run the response through the duckdb connection to create the view
        if isinstance(query, str):
            sqlcode = query.split("```sql")[1].strip("```").strip() if '```sql' in query else query.strip()
        else:
            raise TypeError("Query must be a string")

        tries = 0
        while tries < debug_tries:
            try:
                assert sqlcode.strip().startswith("CREATE VIEW")
                result = self.connection.execute(sql.text(sqlcode))
                break
            except AssertionError as eas:
                logger.error(f"{eas} error: Query is not a CREATE VIEW CODE: {sqlcode[:100]},\n debugging the code")
                sqlcode = self.debug_query(sqlcode, table_name)
                tries += 1
            except Exception as e:
                logger.error(f"{e} Error running query: {sqlcode[:100]},\n debugging the code")
                sqlcode = self.debug_query(sqlcode, table_name)
                tries += 1

        return sqlcode

    def debug_query(self, query: str, table_name: str) -> List[Tuple[str, Any]]:
        """
        Debug a query by running it through the database connection
        :param query: SQL query to run
        :return: result of the query
        """
        LM = LangModel(model='codegemma')
        question = (f"Given the following defective SQL query of table {table_name}, please fix its bugs and return a working version"\
                    f"Return pure, complete SQL code without explanatory text:\n\n{query}")
        # print(self.table_descriptions[table_name])
        response = LM.get_response(question, self.table_descriptions[table_name])
        new_code = response if isinstance(response, str) else response['response']
        new_code = new_code.split("```")[1].strip("```").strip() if '```' in new_code else new_code.strip()
        return new_code
def get_duckdb_connection(url: str) -> object:
    """
    Returns a connection to a duckdb database
    :param url: URL to the duckdb database
    :return: duckdb connection object
    """
    if url == 'duckdb:///:memory:':
        return duckdb.connect()
    else: # for persistent databases
        pth = url.split(":///")[1]
        return duckdb.connect(pth)




def get_csv_description(file_path: str) -> List[Tuple[str, Any]]:
    """
    Returns the description of the csv file using duckdb
    :param file_path: file path or URL for remote file
    :return: list of tuples
    """
    mdb = duckdb.connect()
    query = f"DESCRIBE TABLE '{file_path}';"
    result = mdb.execute(query).fetchall()
    return result