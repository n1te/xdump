# coding: utf-8
import zipfile

import pytest

from .conftest import DATABASE, EMPLOYEES_SQL


pytestmark = pytest.mark.usefixtures('schema')


def test_dump_schema(backend, db_helper):
    """
    Schema should not include any COPY statements.
    """
    output = backend.dump_schema()
    db_helper.assert_schema(output)


@pytest.mark.parametrize('sql, expected', (
    ('', b'id,name\n1,Admin\n2,User\n'),
    ('INSERT INTO groups (id, name) VALUES (3, \'test\')', b'id,name\n1,Admin\n2,User\n3,test\n'),
))
@pytest.mark.usefixtures('schema', 'data')
def test_export_to_csv(backend, cursor, sql, expected):
    if sql:
        cursor.execute(sql)
    assert backend.export_to_csv('SELECT * FROM groups') == expected


class TestRecreating:

    def test_drop_database(self, backend, db_helper):
        original_dbname = backend.dbname
        backend.dbname = 'postgres'
        if DATABASE == 'postgres':
            backend.drop_connections(original_dbname)
        backend.drop_database(original_dbname)
        assert not db_helper.is_database_exists(original_dbname)

    def test_create_database(self, backend, db_helper):
        dbname = db_helper.get_new_database_name()
        backend.create_database(dbname, backend.user)
        assert db_helper.is_database_exists(dbname)

    @pytest.mark.usefixtures('schema', 'data')
    def test_recreate_database(self, backend, db_helper):
        backend.recreate_database()
        assert db_helper.is_database_exists(backend.dbname)

    def test_non_existent_db(self, backend, db_helper):
        assert backend.drop_database('not_exists') is None
        assert not db_helper.is_database_exists('not_exists')


class TestHighLevelInterface:
    """
    Creating a dump and loading it.
    """

    @pytest.fixture
    def dump(self, backend, archive_filename, data):
        backend.dump(archive_filename, ['groups'], {'employees': EMPLOYEES_SQL})

    @pytest.mark.usefixtures('schema', 'dump')
    def test_dump(self, db_helper, archive_filename):
        db_helper.assert_dump(archive_filename)

    @pytest.mark.usefixtures('schema', 'data')
    def test_transaction(self, backend, archive_filename, db_helper):
        """
        We add extra values to the second table after first table was dumped.
        This data should not appear in the result.
        """
        insert = 'INSERT INTO groups (id, name) VALUES (3,\'test\')'

        with db_helper.concurrent_insert(insert):
            backend.dump(archive_filename, ['employees', 'groups'], {})
            archive = zipfile.ZipFile(archive_filename)
            db_helper.assert_groups(archive)
        if DATABASE == 'sqlite':
            backend.run(insert)
        else:
            backend.cache_clear()
        assert backend.run('SELECT COUNT(*) AS "count" FROM groups')[0]['count'] == 3

    @pytest.mark.usefixtures('schema', 'data', 'dump')
    def test_load(self, backend, archive_filename, db_helper):
        backend.recreate_database()
        backend.load(archive_filename)
        assert db_helper.get_tables_count() == 3
        assert backend.run('SELECT name FROM groups') == [{'name': 'Admin'}, {'name': 'User'}]
        if DATABASE == 'postgres':
            result = backend.run("SELECT currval('groups_id_seq')")
            assert result[0]['currval'] == 2


def test_write_schema(backend, db_helper, archive):
    backend.write_schema(archive)
    schema = archive.read('dump/schema.sql')
    db_helper.assert_schema(schema)


@pytest.mark.usefixtures('schema', 'data')
def test_write_partial_tables(backend, archive, db_helper):
    """
    Here we need to select two latest employees with all related managers.
    In that case - John Black will not be in the output.
    """
    backend.write_partial_tables(archive, {'employees': EMPLOYEES_SQL})
    db_helper.assert_employees(archive)


@pytest.mark.usefixtures('schema', 'data')
def test_write_full_tables(backend, archive, db_helper):
    backend.write_full_tables(archive, ['groups'])
    db_helper.assert_groups(archive)
    assert archive.namelist() == ['dump/data/groups.csv']


EMPLOYEES_HEADER = b'id,first_name,last_name,manager_id,referrer_id,group_id'
TICKETS_HEADER = b'id,author_id,subject,message'
DOE = b'1,John,Doe,,,1'
BLACK = b'2,John,Black,1,,1'
SMITH = b'3,John,Smith,1,,1'
BROWN = b'4,John,Brown,3,,2'
SNOW = b'5,John,Snow,3,4,2'


class TestAutoSelect:

    @pytest.fixture(autouse=True)
    def setup(self, request, backend, archive_filename, db_helper, schema, data):
        config = request.node.get_marker('dump')
        backend.dump(archive_filename, *config.args)
        self.archive = zipfile.ZipFile(archive_filename)
        self.db_helper = db_helper

    def assert_content(self, table, expected):
        self.db_helper.assert_content(self.archive, table, expected)

    def assert_employee(self):
        self.assert_content('employees', {EMPLOYEES_HEADER, DOE})

    def assert_group(self):
        self.assert_content('groups', {b'id,name', b'1,Admin'})

    def assert_all_groups(self):
        self.db_helper.assert_groups(self.archive)

    @pytest.mark.dump([], {'employees': 'SELECT * FROM employees WHERE id = 1'})
    def test_related_table(self):
        """
        Selects group related to the given employee.
        """
        self.assert_employee()
        self.assert_group()

    @pytest.mark.dump([], {'employees': 'SELECT * FROM employees WHERE id = 1 LIMIT 1'})
    def test_complex_query(self):
        """
        Input query could contain LIMIT / OFFSET, etc.
        """
        self.assert_employee()
        self.assert_group()

    @pytest.mark.dump(['groups'], {'employees': 'SELECT * FROM employees WHERE id = 1'})
    def test_full_tables_handling(self):
        """
        If all groups are dumped via ``full_tables``, then don't process them separately.
        """
        self.assert_employee()
        self.assert_all_groups()

    @pytest.mark.dump([], {'tickets': 'SELECT * FROM tickets WHERE id = 1'})
    def test_long_relation(self):
        """
        Objects, that are related to related objects should also be selected.
        """
        self.assert_content('tickets', {TICKETS_HEADER, b'1,1,Sub 1,Message 1'})
        self.assert_employee()
        self.assert_group()

    @pytest.mark.dump(['employees'], {})
    def test_related_to_full(self):
        """
        Selection of related objects should work for all tables in ``full_tables`` as well.
        """
        self.assert_all_groups()

    @pytest.mark.dump([], {'employees': 'SELECT * FROM employees WHERE id = 2'})
    def test_recursive_relation(self):
        """
        Self-referencing relations should also be handled.
        """
        self.assert_content('employees', {EMPLOYEES_HEADER, BLACK, DOE})
        self.assert_group()

    @pytest.mark.dump([], {'tickets': 'SELECT * FROM tickets WHERE id = 2'})
    def test_long_recursive_relation(self):
        """
        If related objects have self-referencing relations, it should work as well.
        """
        self.assert_content('tickets', {TICKETS_HEADER, b'2,2,Sub 2,Message 2'})
        self.assert_content('employees', {EMPLOYEES_HEADER, BLACK, DOE})
        self.assert_group()

    @pytest.mark.dump(
        [], {'tickets': 'SELECT * FROM tickets WHERE id = 1', 'employees': 'SELECT * FROM employees WHERE id = 2'}
    )
    def test_multiple_partials(self):
        """
        If different entries from ``partial_tables`` have references to the same relation, then output should contain
        all data required for all mentioned entries without doubling.
        """
        self.assert_content('tickets', {TICKETS_HEADER, b'1,1,Sub 1,Message 1'})
        self.assert_content('employees', {EMPLOYEES_HEADER, BLACK, DOE})
        self.assert_group()

    @pytest.mark.dump(
        [], {'tickets': 'SELECT * FROM tickets WHERE id = 3', 'employees': 'SELECT * FROM employees WHERE id = 5'}
    )
    def test_multiple_partials_with_intersections(self):
        self.assert_content('tickets', {TICKETS_HEADER, b'3,2,Sub 3,Message 3'})
        self.assert_content('employees', {EMPLOYEES_HEADER, SNOW, BROWN, SMITH, DOE, BLACK})
        self.assert_all_groups()

    @pytest.mark.dump([], {'employees': 'SELECT * FROM employees WHERE id = 5'})
    def test_multiple_recursive_relations(self):
        self.assert_content('employees', {EMPLOYEES_HEADER, SNOW, BROWN, SMITH, DOE})
        self.assert_all_groups()
