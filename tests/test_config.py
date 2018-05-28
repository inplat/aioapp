import tempfile
from collections import OrderedDict
import pytest
from aioapp.config import Config, Val, ConfigError


def test_config():
    tmp = tempfile.NamedTemporaryFile()

    class CustomVal(Val):

        def __init__(self, name, value, compare):
            super().__init__(name, value)
            self.compare = compare

        def __call__(self) -> bool:
            return self.value == self.compare

    class Conf(Config):
        db_dsn: str
        db_dsn_def: str
        ison: bool
        isoff: bool
        isinton: bool
        isnotoff: bool
        intval: int
        floatval: float
        floatval_def: float
        temp_path: str
        temp_file: str
        custom: bool
        _vars = {
            'db_dsn': {
                'type': str,
                'name': 'DB_DSN',
                'default': 'postgres://user@localhost/db',
                'descr': 'Database connection string'
            },
            'db_dsn_def': {
                'type': str,
                'name': 'DB_DSN_DEF',
                'default': 'postgres://user@localhost/db',
            },
            'ison': {
                'type': bool,
                'name': 'IS_ON',
            },
            'isoff': {
                'type': bool,
                'name': 'IS_OFF',
                'default': True
            },
            'isinton': {
                'type': bool,
                'name': 'IS_INT_ON',
                'default': False
            },
            'isnotoff': {
                'type': bool,
                'name': 'IS_NOT_OFF',
                'default': True
            },
            'intval': {
                'type': int,
                'name': 'INTVAL',
                'default': 0
            },
            'floatval': {
                'type': float,
                'name': 'FLOATVAL',
                'default': 0.
            },
            'floatval_def': {
                'type': float,
                'name': 'FLOATVAL_DEF',
                'required': False,
            },
            'temp_path': {
                'type': 'dir',
                'name': 'TEMP_PATH',
            },
            'temp_file': {
                'type': 'file',
                'name': 'TEMP_FILE',
            },
            'custom': {
                'type': CustomVal,
                'name': 'CUSTOM',
                'compare': 'ok',
            }
        }

    conf = Conf({
        'DB_DSN': 'postgres://postgres@localhost/postgres',
        'IS_ON': 'On',
        'IS_OFF': 'Off',
        'IS_INT_ON': 1,
        'INTVAL': 1,
        'FLOATVAL': 1.0,
        'TEMP_PATH': tempfile.tempdir,
        'TEMP_FILE': tmp.name,
        'CUSTOM': 'ok',
    })

    assert conf.db_dsn == 'postgres://postgres@localhost/postgres'
    assert conf.db_dsn_def == 'postgres://user@localhost/db'
    assert conf.ison is True
    assert conf.isoff is False
    assert conf.isinton is True
    assert conf.isnotoff is True
    assert conf.intval == 1
    assert conf.floatval == 1.0
    assert conf.floatval_def is None
    assert conf.temp_path == tempfile.tempdir
    assert conf.custom is True
    assert conf.temp_file == tmp.name


def test_config_invalid_str():
    class Conf(Config):
        some_var: str
        _vars = {
            'some_var': {
                'type': str,
                'name': 'SOME_VAR',
                'min': 5,
                'max': 10,
                'required': True
            },
        }

    with pytest.raises(ConfigError, match='.*greater than or equal.*'):
        Conf({'SOME_VAR': '1234'})

    with pytest.raises(ConfigError, match='.*less than or equal.*'):
        Conf({'SOME_VAR': '12345678901'})

    with pytest.raises(ConfigError, match='.*must be a string.*'):
        Conf({'SOME_VAR': True})

    with pytest.raises(ConfigError, match='.*is required.*'):
        Conf({})


def test_config_invalid_bool():
    class Conf(Config):
        some_var: bool
        _vars = {
            'some_var': {
                'type': bool,
                'name': 'SOME_VAR',
                'required': True
            },
        }

    with pytest.raises(ConfigError, match='.*must be a boolean.*'):
        Conf({'SOME_VAR': 'Enabled'})

    with pytest.raises(ConfigError, match='.*must be a boolean.*'):
        Conf({'SOME_VAR': pytest})

    with pytest.raises(ConfigError, match='.*is required.*'):
        Conf({})


def test_config_invalid_int():
    class Conf(Config):
        some_var: int
        _vars = {
            'some_var': {
                'type': int,
                'name': 'SOME_VAR',
                'min': 5,
                'max': 10,
                'required': True
            },
        }

    with pytest.raises(ConfigError, match='.*greater than or equal.*'):
        Conf({'SOME_VAR': 4})

    with pytest.raises(ConfigError, match='.*less than or equal.*'):
        Conf({'SOME_VAR': 11})

    with pytest.raises(ConfigError, match='.*must be an integer.*'):
        Conf({'SOME_VAR': pytest})

    with pytest.raises(ConfigError, match='.*is required.*'):
        Conf({})


def test_config_invalid_float():
    class Conf(Config):
        some_var: float
        _vars = {
            'some_var': {
                'type': float,
                'name': 'SOME_VAR',
                'min': 5,
                'max': 10,
                'required': True
            },
        }

    with pytest.raises(ConfigError, match='.*greater than or equal.*'):
        Conf({'SOME_VAR': 4})

    with pytest.raises(ConfigError, match='.*less than or equal.*'):
        Conf({'SOME_VAR': 11})

    with pytest.raises(ConfigError, match='.*must be a float.*'):
        Conf({'SOME_VAR': pytest})

    with pytest.raises(ConfigError, match='.*is required.*'):
        Conf({})


def test_config_invalid_file():
    class Conf(Config):
        some_var: str
        _vars = {
            'some_var': {
                'type': 'file',
                'name': 'SOME_VAR',
                'required': True
            },
        }

    with pytest.raises(ConfigError, match='.*Could not access to file.*'):
        Conf({'SOME_VAR': 'asdfaur343423//.,,'})

    with pytest.raises(ConfigError, match='.*is required.*'):
        Conf({})


def test_config_invalid_dir():
    class Conf(Config):
        some_var: str
        _vars = {
            'some_var': {
                'type': 'dir',
                'name': 'SOME_VAR',
                'required': True
            },
        }

    with pytest.raises(ConfigError, match='.*does not exist.*'):
        Conf({'SOME_VAR': 'asdfaur343423//.,,'})

    with pytest.raises(ConfigError, match='.*is required.*'):
        Conf({})


def test_config_invalid_custom():
    class CustomVal(Val):

        def __call__(self) -> bool:
            raise Exception('test_custom')

    class Conf(Config):
        some_var: str
        _vars = {
            'some_var': {
                'type': CustomVal,
                'name': 'SOME_VAR'
            },
        }

    with pytest.raises(Exception, match='.*test_custom.*'):
        Conf({'SOME_VAR': 'test'})


def test_config_invalid_config():
    class Conf(Config):
        some_var: str
        _vars = {
            'some_var': {
                'type': 123,
                'name': 'SOME_VAR'
            },
        }

    with pytest.raises(Exception, match='.*Invalid configuration settings.*'):
        Conf({'SOME_VAR': 'test'})


def test_config_as_markdown():
    class Conf(Config):
        _vars = OrderedDict((
            (
                'str_var',
                {
                    'type': str,
                    'name': 'STR_VAR',
                    'default': 'some text',
                    'min': 1,
                    'max': 255,
                    'descr': 'some descr'
                }),
            (
                'int_var',
                {
                    'type': int,
                    'name': 'INT_VAR',
                    'min': 10,
                    'max': 20,
                }),
            (
                'float_var',
                {
                    'type': float,
                    'name': 'FLOAT_VAR',
                    'min': 10,
                    'max': 20,
                }),
            (
                'bool_var',
                {
                    'type': bool,
                    'name': 'BOOL_VAR',
                }),
            (
                'file_var',
                {
                    'type': 'file',
                    'name': 'FILE_VAR',
                }),
            (
                'dir_var',
                {
                    'type': 'dir',
                    'name': 'DIR_VAR',
                    'descr': 'path to directory'
                }),
        ))

    result = '''\
* STR_VAR: some descr
  type: string
  default: some text
  min length: 1
  max length: 255

* INT_VAR
  type: integer
  min value: 10
  max value: 20

* FLOAT_VAR
  type: float
  min value: 10
  max value: 20

* BOOL_VAR
  type: boolean

* FILE_VAR
  type: string(path to file)
  assess mode: r
  encoding: UTF-8

* DIR_VAR: path to directory
  type: string(path to dir)
'''

    assert result == Conf.as_markdown()
