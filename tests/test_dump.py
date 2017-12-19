import zipfile
from unittest.mock import patch

import pytest

from concopy.dump import Dump


pytestmark = pytest.mark.usefixtures('schema')


EMPLOYEES_SQL = '''
WITH RECURSIVE employees_cte AS (
  SELECT * 
  FROM recent_employees
  UNION
  SELECT E.*
  FROM employees E
  INNER JOIN employees_cte ON (employees_cte.manager_id = E.id)
), recent_employees AS (
  SELECT * 
  FROM employees
  WHERE id > 3
)
SELECT * FROM employees_cte
'''


def assert_schema(schema, dumper):
    assert b'Dumped from database version 10.1' in schema
    assert b'CREATE TABLE groups' in schema
    selectable_tables = dumper.get_selectable_tables()
    for table in selectable_tables:
        assert f'COPY {table}'.encode() not in schema


def test_pg_dump(dumper):
    output = dumper.pg_dump()
    assert b'Dumped from database version 10.1' in output
    assert b'CREATE TABLE groups' in output
    selectable_tables = dumper.get_selectable_tables()
    for table in selectable_tables:
        assert f'COPY {table}'.encode() in output


def test_get_selectable_tables(dumper):
    assert dumper.get_selectable_tables() == ['groups', 'employees', 'tickets']


def test_dump_schema(dumper):
    """
    Schema should not include any COPY statements.
    """
    output = dumper.dump_schema()
    assert_schema(output, dumper)


def test_get_sequences(dumper):
    assert dumper.get_sequences() == ['groups_id_seq', 'employees_id_seq', 'tickets_id_seq']


@pytest.mark.parametrize('sql, expected', (
    ('INSERT INTO groups (name) VALUES (\'test\')', 1),
    ('INSERT INTO groups (name) VALUES (\'test\'), (\'test2\')', 2),
))
def test_dump_sequences(dumper, cursor, sql, expected):
    cursor.execute(sql)
    assert f"SELECT pg_catalog.setval('groups_id_seq', {expected}, true);".encode() in dumper.dump_sequences()


@pytest.mark.parametrize('sql, expected', (
    ('', b'id,name\n'),
    ('INSERT INTO groups (name) VALUES (\'test\')', b'id,name\n1,test\n'),
))
def test_export_to_csv(dumper, cursor, sql, expected):
    if sql:
        cursor.execute(sql)
    assert dumper.export_to_csv('SELECT * FROM groups') == expected


class TestDump:

    @pytest.fixture
    def archive_filename(self, tmpdir):
        return str(tmpdir.join('dump.zip'))

    @pytest.fixture
    def archive(self, archive_filename):
        with Dump(archive_filename, 'w', zipfile.ZIP_DEFLATED) as file:
            yield file

    def test_write_schema(self, dumper, archive):
        dumper.write_schema(archive)
        schema = archive.read('dump/schema.sql')
        assert_schema(schema, dumper)

    def test_write_sequences(self, dumper, archive):
        dumper.write_sequences(archive)
        assert "SELECT pg_catalog.setval('groups_id_seq', 1, false);".encode() in archive.read('dump/sequences.sql')

    def test_write_full_tables(self, dumper, archive):
        dumper.write_full_tables(archive, ['groups'])
        assert archive.read('dump/data/groups.csv') == b'id,name\n'
        assert archive.namelist() == ['dump/data/groups.csv']

    def test_pg_dump_environment(self, dumper):
        dumper.password = 'PASSW'
        assert dumper.pg_dump_environment['PGPASSWORD'] == dumper.password

    def test_pg_dump_environment_empty_password(self, dumper):
        assert 'PGPASSWORD' not in dumper.pg_dump_environment

    def test_dump(self, dumper, archive_filename):
        dumper.dump(archive_filename, ['groups'], {'employees': EMPLOYEES_SQL})
        archive = zipfile.ZipFile(archive_filename)
        assert archive.namelist() == [
            'dump/schema.sql', 'dump/sequences.sql', 'dump/data/groups.csv', 'dump/data/employees.csv',
        ]
        assert archive.read('dump/data/groups.csv') == b'id,name\n'
        assert archive.read('dump/data/employees.csv') == b'id,first_name,last_name,manager_id,group_id\n'
        assert "SELECT pg_catalog.setval('groups_id_seq', 1, false);".encode() in archive.read('dump/sequences.sql')
        schema = archive.read('dump/schema.sql')
        assert_schema(schema, dumper)

    def test_transaction(self, dumper, cursor, archive_filename):
        is_added = False

        def add_group(*args, **kwargs):
            nonlocal is_added
            if not is_added:
                cursor.execute('INSERT INTO groups (name) VALUES (\'test\')')
                is_added = True

        with patch.object(dumper, 'export_to_csv', wraps=dumper.export_to_csv, side_effect=add_group):
            dumper.dump(archive_filename, ['employees', 'groups'])
            archive = zipfile.ZipFile(archive_filename)
            assert archive.read('dump/data/groups.csv') == b'id,name\n'

    @pytest.mark.usefixtures('schema', 'data')
    def test_write_partial_tables(self, dumper, archive):
        """
        Here we need to select two latest employees with all related managers.
        In that case - John Black will not be in the output.
        """

        dumper.write_partial_tables(archive, {'employees': EMPLOYEES_SQL})
        assert archive.read('dump/data/employees.csv') == b'id,first_name,last_name,manager_id,group_id\n' \
                                                          b'5,John,Snow,3,2\n' \
                                                          b'4,John,Brown,3,2\n' \
                                                          b'3,John,Smith,1,1\n' \
                                                          b'1,John,Doe,,1\n'

    def test_load_related_objects(self, dumper, archive):
        dumper.write_partial_tables(archive, {'employees': EMPLOYEES_SQL}, ['tickets'])
