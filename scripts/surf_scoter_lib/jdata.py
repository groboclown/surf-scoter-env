#SPDX:MIT
"""Handlers for JSON-like data."""
import json
from collections.abc import Sequence
from enum import StrEnum
from typing import NoReturn


class Found(StrEnum):
    """The result of finding an element in the structure."""
    OK = "ok"  # It was found.
    EMPTY = "empty"  # Nothing was there (different than 'null')
    WRONG_PATH = "wrong_path"  # Path didn't match up with the data.
    WRONG_TYPE = "wrong_type"  # Expected the path to have one type, found a different one.

    def __bool__(self) -> bool:
        return self == Found.OK


JsonData = str | int | float | bool | None | list["JsonData"] | dict[str, "JsonData"]
DictJsonData = dict[str, JsonData]
ListJsonData = list[JsonData]
JsonPathPart = str | int
JsonPath = Sequence[JsonPathPart]
OptJson = tuple[JsonData, bool]
OptJsonPath = tuple[JsonData, Found, JsonPath]


def path_to_str(*path: JsonPathPart) -> str:
    """Convert the path to a user-friendly string."""
    return " / ".join([str(p) for p in path])


def path_expr_to_str(path: JsonPath) -> str:
    """Convert the path to a user-friendly string."""
    return path_to_str(*path)


def json_from_str(data: str | bytes) -> JsonData:
    """Load the JSON contents into a structured format."""
    ret: JsonData = json.loads(data)
    return ret


def json_into_str(data: JsonData) -> str:
    """Export the JSON structured data to a UTF-8 formatted string."""
    return json.dumps(data)


def try_str(data: JsonData, *path: JsonPathPart) -> tuple[str | None, Found, JsonPath]:
    """Return the string at the path location."""
    val, ok, fp = get_at(data, *path)
    if ok:
        if isinstance(val, str):
            return (val, ok, fp)
        return (None, Found.WRONG_TYPE, fp)
    return (None, ok, fp)


def opt_str(data: JsonData, *path: JsonPathPart) -> str | None:
    """Return the string at the path location.
    
    If the value didn't exist, it returns None.
    If the value exists but wasn't a dict, raises a TypeError.
    If the path didn't align with expectations, raises a KeyError.
    """
    val, ok, fp = try_str(data, *path)
    if ok:
        return val
    return _as_optional(val, ok, fp, "string")


def req_str(data: JsonData, *path: JsonPathPart) -> str:
    """Require the string at the path."""
    val, ok, fp = try_str(data, *path)
    if ok and val is not None:
        return val
    _as_required(val, ok, fp, "string")


def try_text(data: JsonData, *path: JsonPathPart) -> tuple[str | None, Found, JsonPath]:
    """Return the scalar as a string at the path location, as a string.
    
    Handles string, int, float, and bool.  None is returned as None.
    """
    val, ok, fp = get_at(data, *path)
    if ok:
        if isinstance(val, str | int | float | bool):
            return (str(val), ok, fp)
        return (None, Found.WRONG_TYPE, fp)
    return (None, ok, fp)


def opt_text(data: JsonData, *path: JsonPathPart) -> str | None:
    """Return the scalar as a string at the path location.
    
    If the value didn't exist, it returns None.
    If the value exists but wasn't a dict, raises a TypeError.
    If the path didn't align with expectations, raises a KeyError.
    """
    val, ok, fp = try_text(data, *path)
    if ok:
        return val
    return _as_optional(val, ok, fp, "string")


def try_int(data: JsonData, *path: JsonPathPart) -> tuple[int | None, Found, JsonPath]:
    """Return the scalar as an integer at the path location, as a string.
    
    Handles string, int, float, and bool.  None is returned as None.
    """
    val, ok, fp = get_at(data, *path)
    if ok:
        if isinstance(val, int):
            return (val, ok, fp)
        if isinstance(val, float | str | bool):
            try:
                return (int(val), ok, fp)
            except (ValueError, TypeError):
                # Fall through
                pass
        return (None, Found.WRONG_TYPE, fp)
    return (None, ok, fp)


def opt_int(data: JsonData, *path: JsonPathPart) -> int | None:
    """Return the scalar as an integer at the path location.
    
    If the value didn't exist, it returns None.
    If the value exists but wasn't a dict, raises a TypeError.
    If the path didn't align with expectations, raises a KeyError.
    """
    val, ok, fp = try_int(data, *path)
    if ok:
        return val
    return _as_optional(val, ok, fp, "int")


def req_int(data: JsonData, *path: JsonPathPart) -> int:
    """Require the scalar as an integer at the path."""
    val, ok, fp = try_int(data, *path)
    if ok and val is not None:
        return val
    _as_required(val, ok, fp, "int")


def try_float(data: JsonData, *path: JsonPathPart) -> tuple[float | None, Found, JsonPath]:
    """Return the scalar as a float at the path location, as a string.
    
    Handles string, int, float, and bool.  None is returned as None.
    """
    val, ok, fp = get_at(data, *path)
    if ok:
        if isinstance(val, float):
            return (val, ok, fp)
        if isinstance(val, int | str | bool):
            try:
                return (float(val), ok, fp)
            except (ValueError, TypeError):
                # Fall through
                pass
        return (None, Found.WRONG_TYPE, fp)
    return (None, ok, fp)


def opt_float(data: JsonData, *path: JsonPathPart) -> float | None:
    """Return the scalar as a float at the path location.
    
    If the value didn't exist, it returns None.
    If the value exists but wasn't a dict, raises a TypeError.
    If the path didn't align with expectations, raises a KeyError.
    """
    val, ok, fp = try_float(data, *path)
    if ok:
        return val
    return _as_optional(val, ok, fp, "int")


def req_float(data: JsonData, *path: JsonPathPart) -> float:
    """Require the scalar as a float at the path."""
    val, ok, fp = try_float(data, *path)
    if ok and val is not None:
        return val
    _as_required(val, ok, fp, "float")


def try_bool(data: JsonData, *path: JsonPathPart) -> tuple[bool | None, Found, JsonPath]:
    """Return the scalar as a boolean at the path location, as a string.
    
    Handles string, int, float, and bool.  None is returned as None.
    """
    val, ok, fp = get_at(data, *path)
    if ok:
        if isinstance(val, bool):
            return (val, ok, fp)
        if isinstance(val, float | str | bool):
            try:
                return (bool(val), ok, fp)
            except (ValueError, TypeError):
                # Fall through
                pass
        return (None, Found.WRONG_TYPE, fp)
    return (None, ok, fp)


def opt_bool(data: JsonData, *path: JsonPathPart) -> bool | None:
    """Return the scalar as a boolean at the path location.
    
    If the value didn't exist, it returns None.
    If the value exists but wasn't a dict, raises a TypeError.
    If the path didn't align with expectations, raises a KeyError.
    """
    val, ok, fp = try_bool(data, *path)
    if ok:
        return val
    return _as_optional(val, ok, fp, "true or false")


def req_bool(data: JsonData, *path: JsonPathPart) -> bool:
    """Require the scalar as a boolean at the path."""
    val, ok, fp = try_bool(data, *path)
    if ok and val is not None:
        return val
    _as_required(val, ok, fp, "int")


def try_dict(data: JsonData, *path: JsonPathPart) -> tuple[DictJsonData | None, Found, JsonPath]:
    """Return the dictionary at the path location."""
    val, ok, fp = get_at(data, *path)
    if ok:
        if isinstance(val, dict):
            return (val, ok, fp)
        return (None, Found.WRONG_TYPE, fp)
    return (None, ok, fp)


def opt_dict(data: JsonData, *path: JsonPathPart) -> DictJsonData | None:
    """Return the dictionary at the path location.
    
    If the value didn't exist, it returns None.
    If the value exists but wasn't a dict, raises a TypeError.
    If the path didn't align with expectations, raises a KeyError.
    """
    val, ok, fp = try_dict(data, *path)
    if ok:
        return val
    return _as_optional(val, ok, fp, "map")


def req_dict(data: JsonData, *path: JsonPathPart) -> DictJsonData:
    """Require the dictionary at the path."""
    val, ok, fp = try_dict(data, *path)
    if ok and val is not None:
        return val
    _as_required(val, ok, fp, "map")


def opt_dict_keys(data: JsonData, *path: JsonPathPart) -> Sequence[JsonPath]:
    """Return the paths under the dictionary.
    
    If the value didn't exist, it returns None.
    If the value exists but wasn't a dict, raises a TypeError.
    If the path didn't align with expectations, raises a KeyError.
    """
    val = opt_dict(data, *path)
    if val is None:
        return []
    ret: list[JsonPath] = []
    for key in val:
        ret.append((*path, key))
    return ret


def try_list(data: JsonData, *path: JsonPathPart) -> tuple[ListJsonData | None, Found, JsonPath]:
    """Return the list at the path location."""
    val, ok, fp = get_at(data, *path)
    if ok:
        if isinstance(val, list):
            return (val, ok, fp)
        return (None, Found.WRONG_TYPE, fp)
    return (None, ok, fp)


def opt_list(data: JsonData, *path: JsonPathPart) -> ListJsonData | None:
    """Return the list at the path location.
    
    If the value didn't exist, it returns None.
    If the value exists but wasn't a list, raises a TypeError.
    If the path didn't align with expectations, raises a KeyError.
    """
    val, ok, fp = try_list(data, *path)
    if ok:
        return val
    return _as_optional(val, ok, fp, "list")


def req_list(data: JsonData, *path: JsonPathPart) -> ListJsonData:
    """Require a list at the path."""
    val, ok, fp = try_list(data, *path)
    if ok and val is not None:
        return val
    _as_required(val, ok, fp, "map")


def opt_list_keys(data: JsonData, *path: JsonPathPart) -> Sequence[JsonPath]:
    """Return the paths under the list.
    
    If the value didn't exist, it returns None.
    If the value exists but wasn't a dict, raises a TypeError.
    If the path didn't align with expectations, raises a KeyError.
    """
    val = opt_list(data, *path)
    if val is None:
        return []
    ret: list[JsonPath] = []
    for idx in range(len(val)):
        ret.append((*path, idx))
    return ret


def optional(data: JsonData, *path: JsonPathPart) -> JsonData:
    """Require something at the data point.
    
    It can be None if the value there is null or doesn't exist.
    """
    val, ok, fp = get_at(data, *path)
    return _as_optional(val, ok, fp, "value")


def get_at(data: JsonData, *path: JsonPathPart) -> OptJsonPath:
    """Extract the value from the data at the given path.
    
    This follows list and dict values through the path.

    Returns:
        0: the discovered value (None if not found)
        1: The discovery result
        2: The final path where this search terminated.
    """
    curr: JsonData = data
    followed: list[JsonPathPart] = []
    for part in path:
        followed.append(part)
        if isinstance(curr, list):
            if isinstance(part, int) and 0 <= part < len(curr):
                curr = curr[part]
                continue
            return (None, Found.WRONG_PATH, followed)
        if isinstance(curr, dict):
            if isinstance(part, str) and part in curr:
                curr = curr[part]
                continue
            return (None, Found.WRONG_PATH, followed)
        return (curr, Found.EMPTY, followed)
    return (curr, Found.OK, followed)


def _type_err(val: JsonData, path: JsonPath, exp_type: str) -> TypeError:
    return TypeError(f"{path_expr_to_str(path)}: expected {exp_type}, found {type(val)}")


def _key_err(path: JsonPath) -> KeyError:
    return KeyError(f"{path_expr_to_str(path)}: no value")


def _as_optional[T: JsonData](val: T, ok: Found, fp: JsonPath, expected: str) -> T | None:
    if ok == Found.OK:
        return val
    if ok == Found.EMPTY:
        return None
    if ok == Found.WRONG_TYPE:
        raise _type_err(val, fp, expected)
    if ok == Found.WRONG_PATH:
        raise _key_err(fp)
    raise RuntimeError(f"unknown Found type {ok}")


def _as_required(val: JsonData, ok: Found, fp: JsonPath, expected: str) -> NoReturn:
    if ok == Found.EMPTY:
        raise ValueError(f"{path_expr_to_str(fp)}: required a {expected}")
    if ok == Found.WRONG_TYPE:
        raise _type_err(val, fp, expected)
    if ok == Found.WRONG_PATH:
        raise _key_err(fp)
    raise RuntimeError(f"unknown Found type {ok}")
