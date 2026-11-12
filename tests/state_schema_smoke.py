"""校验 Threadweave 状态 schema 与示例的一致性。

使用标准库实现 schema 子集的参考校验器（不依赖 jsonschema 包），覆盖 required、
type、enum、const、pattern、minLength、minimum、format=date-time、additionalProperties、
items、properties 与 anyOf。该脚本证明示例符合声明的 schema，并验证若干非法状态会被拒绝；
它不是完整 JSON Schema 规范的实现，也不能替代真实运行时的状态写入校验。
"""
import copy
import json
import re
from pathlib import Path

project = Path(__file__).resolve().parents[1]
results = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    results.append(name)


def is_type(value, t):
    return {
        'string': lambda v: isinstance(v, str),
        'integer': lambda v: isinstance(v, int) and not isinstance(v, bool),
        'boolean': lambda v: isinstance(v, bool),
        'array': lambda v: isinstance(v, list),
        'object': lambda v: isinstance(v, dict),
        'null': lambda v: v is None,
    }[t](value)


DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$')


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
            for k in value:
                if k not in props:
                    errors.append(f'{path}: unexpected {k}')
        for k, v in value.items():
            if k in props:
                errors.extend(validate(v, props[k], f'{path}.{k}'))
    if isinstance(value, list) and 'items' in schema:
        for i, item in enumerate(value):
            errors.extend(validate(item, schema['items'], f'{path}[{i}]'))
    return errors


schema = json.loads((project / 'references/state.schema.json').read_text(encoding='utf-8'))
example = json.loads((project / 'references/state.example.json').read_text(encoding='utf-8'))

check('schema-json-valid', isinstance(schema, dict) and schema.get('type') == 'object')
check('example-valid-against-schema', not validate(example, schema))
check('schema-version-matches', example['schemaVersion'] == schema['properties']['schemaVersion']['const'])


def mutated(mutation):
    obj = copy.deepcopy(example)
    mutation(obj)
    return obj


def rejected(name, mutation, keyword):
    errors = validate(mutated(mutation), schema)
    check(name, any(keyword in e for e in errors))


rejected('missing-required-rejected', lambda o: o.pop('runId'), 'missing runId')
rejected('wrong-const-rejected', lambda o: o.__setitem__('schemaVersion', '2.0'), 'expected const')
rejected('invalid-enum-rejected', lambda o: o.__setitem__('status', 'unknown'), 'not in enum')
rejected('invalid-base-sha-rejected',
         lambda o: o['integration'].__setitem__('baseSha', 'abc'), 'pattern mismatch')
rejected('invalid-timestamp-rejected',
         lambda o: o.__setitem__('createdAt', 'not-a-time'), 'invalid date-time')
rejected('invalid-attempt-rejected',
         lambda o: o['workers'][0].__setitem__('attempt', 0), 'below minimum')
rejected('unexpected-field-rejected',
         lambda o: o.__setitem__('unexpected', True), 'unexpected unexpected')

identities = [(w['role'], w['attempt']) for w in example['workers']]
check('worker-role-attempt-unique', len(identities) == len(set(identities)))
check('merge-order-matches-log',
      example['mergeOrder'] == [x['branch'] for x in example['integrationLog']])

print(json.dumps({
    'passed': len(results),
    'checks': results,
    'scope': 'State schema and example only; no runtime state writes',
}, ensure_ascii=False, indent=2))
