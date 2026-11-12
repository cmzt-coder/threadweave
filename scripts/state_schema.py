"""Threadweave 状态 schema 轻量校验器（标准库，不依赖 jsonschema）。

支持本 schema 使用的子集：required、type、enum、const、pattern、minLength、
minimum、format=date-time、additionalProperties、items、properties 与 anyOf。
"""
import json
import re
from pathlib import Path

DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$')


def is_type(value, t):
    return {
        'string': lambda v: isinstance(v, str),
        'integer': lambda v: isinstance(v, int) and not isinstance(v, bool),
        'boolean': lambda v: isinstance(v, bool),
        'array': lambda v: isinstance(v, list),
        'object': lambda v: isinstance(v, dict),
        'null': lambda v: v is None,
    }[t](value)


def validate(value, schema, path='$'):
    errors = []
    if not isinstance(schema, dict):
        return errors
    if 'anyOf' in schema:
        branches = [validate(value, s, path) for s in schema['anyOf']]
        return [] if any(not e for e in branches) else [f'{path}: anyOf failed']
    t = schema.get('type')
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(is_type(value, x) for x in types):
            return [f'{path}: expected type {t}']
    if 'const' in schema and value != schema['const']:
        errors.append(f'{path}: expected const {schema["const"]!r}')
    if 'enum' in schema and value not in schema['enum']:
        errors.append(f'{path}: not in enum')
    if isinstance(value, str):
        if 'pattern' in schema and not re.search(schema['pattern'], value):
            errors.append(f'{path}: pattern mismatch')
        if 'minLength' in schema and len(value) < schema['minLength']:
            errors.append(f'{path}: too short')
        if schema.get('format') == 'date-time' and not DATE_RE.match(value):
            errors.append(f'{path}: invalid date-time')
    if isinstance(value, int) and not isinstance(value, bool):
        if 'minimum' in schema and value < schema['minimum']:
            errors.append(f'{path}: below minimum')
    if isinstance(value, dict):
        for req in schema.get('required', []):
            if req not in value:
                errors.append(f'{path}: missing {req}')
        props = schema.get('properties', {})
        if schema.get('additionalProperties') is False:
            for key in value:
                if key not in props:
                    errors.append(f'{path}: unexpected {key}')
        for key, item in value.items():
            if key in props:
                errors.extend(validate(item, props[key], f'{path}.{key}'))
    if isinstance(value, list) and 'items' in schema:
        for index, item in enumerate(value):
            errors.extend(validate(item, schema['items'], f'{path}[{index}]'))
    return errors


def load_schema(schema_path):
    return json.loads(Path(schema_path).read_text(encoding='utf-8'))


def validate_invariants(state):
    """校验 schema 无法表达的跨字段领域不变量。"""
    errors = []
    workers = state.get('workers', [])
    identities = [(w.get('role'), w.get('attempt')) for w in workers]
    if len(identities) != len(set(identities)):
        errors.append('duplicate worker role/attempt')
    if state.get('status') == 'complete':
        if state.get('finalReview', {}).get('status') != 'complete':
            errors.append('complete run requires complete finalReview')
        if state.get('cleanup', {}).get('status') != 'complete':
            errors.append('complete run requires complete cleanup')
        active_by_role = {}
        for worker in workers:
            role = worker.get('role')
            attempt = worker.get('attempt', 1)
            if role not in active_by_role or attempt > active_by_role[role].get('attempt', 0):
                active_by_role[role] = worker
        active = [
            w for w in active_by_role.values()
            if w.get('state') not in ('integrated', 'archived')
        ]
        if active:
            errors.append('complete run requires active workers integrated/archived')
        if state.get('cleanup', {}).get('outstandingResources'):
            errors.append('complete run requires no outstanding resources')
    if errors:
        raise ValueError('state invariant violation: ' + '; '.join(errors))


def validate_state(state, schema_path=None):
    """校验 run-state；失败抛 ValueError，避免非法状态落盘。"""
    if schema_path is None:
        schema_path = Path(__file__).resolve().parents[1] / 'references/state.schema.json'
    errors = validate(state, load_schema(schema_path))
    if errors:
        raise ValueError('state schema validation failed: ' + '; '.join(errors))
    validate_invariants(state)
    return state
