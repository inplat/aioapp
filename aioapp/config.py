import copy
import os
from typing import Optional, Union, Dict
from os import _Environ

Env = Union[_Environ, dict]


class ConfigError(Exception):
    pass


class Val:

    def __init__(self, name, value):
        self.name = name
        self.value = value

    def __call__(self):  # pragma: nocover
        raise NotImplementedError()


class StrVal(Val):

    def __call__(self, max: Optional[int] = None,
                 min: Optional[int] = None) -> str:
        if not isinstance(self.value, str):
            raise ConfigError("%s must be a string" % self.name)
        if min is not None and len(self.value) < min:
            raise ConfigError("length of %s must be greater than or equal to "
                              "%s"
                              "" % (self.name, min))
        if max is not None and len(self.value) > max:
            raise ConfigError("length of %s must be less than or equal to %s"
                              "" % (self.name, max))

        return self.value


class BoolVal(Val):

    def __call__(self) -> bool:
        if isinstance(self.value, bool):
            return self.value
        if isinstance(self.value, int):
            return self.value != 0
        if isinstance(self.value, str):
            if self.value.lower() in ('1', 'on', 'true', 't'):
                return True
            if self.value.lower() in ('0', 'off', 'false', 'f'):
                return False
        raise ConfigError("%s must be a boolean" % self.name)


class IntVal(Val):

    def __call__(self, max: Optional[int] = None,
                 min: Optional[int] = None) -> int:
        try:
            val = int(self.value)
        except Exception:
            raise ConfigError("%s must be an integer" % self.name)
        if min is not None and val < min:
            raise ConfigError("%s must be greater than or equal to %s"
                              "" % (self.name, min))
        if max is not None and val > max:
            raise ConfigError("%s must be less than or equal to %s"
                              "" % (self.name, max))
        return val


class FloatVal(Val):

    def __call__(self, max: Optional[float] = None,
                 min: Optional[float] = None) -> float:
        try:
            val = float(self.value)
        except Exception:
            raise ConfigError("%s must be a float" % self.name)
        if min is not None and val < min:
            raise ConfigError("%s must be greater than or equal to %s"
                              "" % (self.name, min))
        if max is not None and val > max:
            raise ConfigError("%s must be less than or equal to %s"
                              "" % (self.name, max))
        return val


class FileVal(Val):

    def __call__(self, mode: str = 'r', encoding: str = 'UTF-8') -> str:
        try:
            with open(self.value, mode, encoding=encoding):
                pass
        except Exception as e:
            raise ConfigError("Could not access to file %s with error: %s"
                              "" % (self.value, e))
        return self.value


class DirVal(Val):

    def __call__(self) -> str:
        if not os.path.exists(self.value):
            raise ConfigError("Directory %s does not exist"
                              "" % self.value)
        return self.value


class Config:
    _vars: Dict[str, Dict] = {}

    def __init__(self,
                 env: Optional[Env] = None) -> None:
        self._env = env or os.environ
        self._conf = copy.deepcopy(self._vars)
        self._description: Dict[str, Dict] = {}
        for key, val in self._conf.items():
            val_type = val.pop('type')
            val_name = val.pop('name')
            val_default = None
            if 'default' in val:
                val_default = val.pop('default')
            not_null = False
            if 'not_null' in val:
                not_null = val.pop('not_null')
            descr = ''
            if 'descr' in val:
                not_null = val.pop('descr')

            self._description[val_name] = {
                'type': val_type,
                'default': val_default,
                'not_null': not_null,
                'descr': descr,
            }

            value = self._env.get(val_name, val_default)
            if value is None:
                if not_null:
                    raise ConfigError("%s must be not null" % val_name)
                else:
                    setattr(self, key, None)
            else:
                if val_type == str:
                    setattr(self, key, StrVal(val_name, value)(**val))
                elif val_type == bool:
                    setattr(self, key, BoolVal(val_name, value)())
                elif val_type == int:
                    setattr(self, key, IntVal(val_name, value)(**val))
                elif val_type == float:
                    setattr(self, key, FloatVal(val_name, value)(**val))
                elif val_type == 'file':
                    setattr(self, key, FileVal(val_name, value)(**val))
                elif val_type == 'dir':
                    setattr(self, key, DirVal(val_name, value)())
                elif isinstance(val_type, type) and issubclass(val_type, Val):
                    setattr(self, key, val_type(val_name, value)(**val))
                else:
                    raise UserWarning('Invalid configuration settings')
