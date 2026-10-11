"""Policy and deterministic validation for admin restaurant onboarding."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CUISINES = json.loads(Path(__file__).with_name('cuisines.json').read_text())
PERMISSIONS = ['restaurant:read', 'onboarding:edit', 'document:read', 'document:review',
               'contract:prepare', 'contract:send', 'onboarding:submit', 'onboarding:approve',
               'restaurant:activate', 'audit:read']
DAYS = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
EDITABLE = {'Draft', 'Changes requested'}


class Problem(Exception):
    def __init__(self, status, code, message, fields=None, revision=None):
        self.status, self.code, self.message = status, code, message
        self.fields, self.revision = fields or [], revision
        super().__init__(message)


def fail(message, path=''):
    raise Problem(422, 'VALIDATION_FAILED', message, [{'path': path, 'message': message}])


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def policy():
    value = {
        'country': 'IN', 'currency': 'INR', 'timezoneCatalog': ['Asia/Kolkata'],
        'requiredFields': ['restaurant.name','owner.fullName','owner.phone','primaryContact.phone',
                           'location.building','location.street','location.area','location.city',
                           'location.state','location.postalCode','location.latitude','location.longitude'],
        'requiredDocuments': ['PAN','FSSAI','bank'], 'requiredImageCategories': ['profile','entrance','interior'],
        'maxCuisines': 8, 'imageBytes': 10485760, 'documentBytes': 20971520,
        'restaurantBytes': 104857600, 'acceptedMenuFormats': ['csv'],
        'mimeTypes': ['image/jpeg','image/png','image/webp','application/pdf','text/csv'],
        'devices': [], 'resubmitRejected': False, 'activeChangeReview': False,
        'contractRequiredForApproval': True, 'manualSignedCopyApproval': True,
        'features': {'geocoding': False, 'externalVerification': False, 'esign': False,
                     'uploads': bool(os.getenv('ONBOARDING_CLAMD_HOST')),
                     'contracts': bool(os.getenv('ONBOARDING_CONTRACT_CONFIG'))},
        'menuSchemaVersion': 'bittu-items-v1', 'duplicateStrategy': 'normalized-category-name',
        'scheduleShape': 'weekday object: {enabled, slots:[{opens,closes,closesNextDay}]}',
        'retention': 'Evidence retained; DELETE detaches draft assets without destroying originals.',
    }
    value['version'] = fingerprint(value)[:16]
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, allow_inf_nan=False)


class Restaurant(Strict):
    name: str = Field('', max_length=255)
    outletCode: str | None = Field(None, max_length=100)


class Owner(Strict):
    fullName: str = Field('', max_length=200)
    phone: str = ''
    email: str | None = Field(None, max_length=254)


class Contact(Strict):
    phone: str = ''
    sameAsOwner: bool = False


class Location(Strict):
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    building: str = Field('', max_length=200)
    floor: str | None = Field(None, max_length=50)
    street: str = Field('', max_length=200)
    area: str = Field('', max_length=200)
    landmark: str | None = Field(None, max_length=200)
    city: str = Field('', max_length=100)
    state: str = Field('', max_length=100)
    postalCode: str = ''
    country: Literal['IN'] = 'IN'


class Food(Strict):
    assetId: str
    caption: str | None = Field(None, max_length=500)
    order: int = Field(0, ge=0)


class Media(Strict):
    profileAssetId: str | None = None
    entranceAssetIds: list[str] = Field(default_factory=list, max_length=20)
    interiorAssetIds: list[str] = Field(default_factory=list, max_length=20)
    food: list[Food] = Field(default_factory=list, max_length=100)


class Slot(Strict):
    opens: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    closes: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    closesNextDay: bool = False


class Day(Strict):
    enabled: bool = False
    slots: list[Slot] = Field(default_factory=list, max_length=12)


class Operations(Strict):
    deliveryEnabled: bool = False
    diningEnabled: bool = False
    useDeliveryMenuForDining: bool = False
    cuisineIds: list[str] = Field(default_factory=list, max_length=8)
    timezone: Literal['Asia/Kolkata'] = 'Asia/Kolkata'
    deviceId: str | None = None
    posDetails: str | None = Field(None, max_length=1000)
    deliverySchedule: dict[str, Day] = Field(default_factory=dict)
    diningSchedule: dict[str, Day] = Field(default_factory=dict)
    deliveryMenuAssetIds: list[str] = Field(default_factory=list, max_length=20)
    diningMenuAssetIds: list[str] = Field(default_factory=list, max_length=20)


class Draft(Strict):
    restaurant: Restaurant = Field(default_factory=Restaurant)
    owner: Owner = Field(default_factory=Owner)
    primaryContact: Contact = Field(default_factory=Contact)
    location: Location = Field(default_factory=Location)
    media: Media = Field(default_factory=Media)
    operations: Operations = Field(default_factory=Operations)


def validate(model, body):
    try:
        return model.model_validate(body).model_dump(mode='json')
    except ValidationError as exc:
        raise Problem(422, 'VALIDATION_FAILED', 'Please correct the highlighted fields', [
            {'path': '.'.join(str(p) for p in e['loc']), 'message': e['msg']} for e in exc.errors()
        ]) from None


def merge(old, patch):
    result = copy.deepcopy(old)
    for key, val in patch.items():
        result[key] = merge(result[key], val) if isinstance(val, dict) and isinstance(result.get(key), dict) else val
    return result


def schedule_valid(schedule, path):
    if set(schedule) - set(DAYS):
        fail('Unknown weekday', path)
    intervals = []
    for idx, day in enumerate(DAYS):
        item = schedule.get(day, {})
        if not item.get('enabled'):
            continue
        for slot in item.get('slots', []):
            def minutes(t):
                h, m = map(int, t.split(':'))
                return h * 60 + m
            start, end = minutes(slot['opens']), minutes(slot['closes'])
            if start == end or (end <= start and not slot['closesNextDay']):
                fail('Invalid interval; overnight closing requires closesNextDay', path + '.' + day)
            if slot['closesNextDay']:
                end += 1440
            if end - start > 1440:
                fail('A slot cannot exceed 24 hours', path + '.' + day)
            intervals.append((idx * 1440 + start, idx * 1440 + end))
    expanded = sorted(intervals + [(a+10080,b+10080) for a,b in intervals])
    if any(b > c for (_, b), (c, _) in zip(expanded, expanded[1:])):
        fail('Schedule slots overlap, including across the week boundary', path)


def validate_draft(data):
    result = validate(Draft, data)
    for section in ('owner', 'primaryContact'):
        phone = result[section]['phone']
        if phone and not re.fullmatch(r'\+[1-9]\d{7,14}', phone):
            fail('Use E.164 phone format, e.g. +919876543210', section + '.phone')
    email = result['owner']['email']
    if email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        fail('Invalid email', 'owner.email')
    if result['primaryContact']['sameAsOwner']:
        result['primaryContact']['phone'] = result['owner']['phone']
    postal = result['location']['postalCode']
    if postal and not re.fullmatch(r'[1-9]\d{5}', postal):
        fail('Postal code must have six digits', 'location.postalCode')
    ops = result['operations']
    if len(set(ops['cuisineIds'])) != len(ops['cuisineIds']) or set(ops['cuisineIds']) - {c['id'] for c in CUISINES}:
        fail('Use distinct IDs from the cuisine catalog', 'operations.cuisineIds')
    if ops['deviceId'] and ops['deviceId'] not in policy()['devices']:
        fail('Device is not configured', 'operations.deviceId')
    for service in ('delivery','dining'):
        schedule_valid(ops[service + 'Schedule'], 'operations.' + service + 'Schedule')
    return result


class Document(Strict):
    assetId: str
    legalName: str | None = Field(None, max_length=200)
    number: str | None = None
    businessName: str | None = Field(None, max_length=200)
    expiry: date | None = None
    holder: str | None = Field(None, max_length=200)
    accountNumber: str | None = None
    ifsc: str | None = None
    bankName: str | None = Field(None, max_length=200)
    branch: str | None = Field(None, max_length=200)


def document_valid(kind, body):
    data = validate(Document, body)
    required = {'PAN': ['legalName','number'], 'FSSAI':['number','businessName','expiry'],
                'bank':['holder','accountNumber','ifsc','bankName','branch']}
    if kind not in required:
        raise Problem(404, 'NOT_FOUND', 'Unknown document kind')
    for field in required[kind]:
        if not data.get(field):
            fail('Required', 'documents.' + kind + '.' + field)
    if kind == 'PAN' and not re.fullmatch(r'[A-Z]{5}[0-9]{4}[A-Z]', data['number']):
        fail('Invalid PAN format', 'documents.PAN.number')
    if kind == 'FSSAI':
        if not re.fullmatch(r'\d{14}', data['number']):
            fail('FSSAI number must have 14 digits', 'documents.FSSAI.number')
        if date.fromisoformat(data['expiry']) <= date.today():
            fail('FSSAI has expired', 'documents.FSSAI.expiry')
    if kind == 'bank':
        if not re.fullmatch(r'[A-Z]{4}0[A-Z0-9]{6}', data['ifsc']):
            fail('Invalid IFSC', 'documents.bank.ifsc')
        if not re.fullmatch(r'\d{6,25}', data['accountNumber']):
            fail('Account number must contain 6–25 digits', 'documents.bank.accountNumber')
    allowed = set(required[kind]) | {'assetId'}
    if any(v is not None and k not in allowed for k,v in data.items()):
        fail('Fields do not belong to this document kind', 'documents.' + kind)
    return {k:v for k,v in data.items() if v is not None}


def readiness(data, status, menu_counts, p=None):
    p = p or policy()
    blockers = []
    def block(path, message, stage='submit'):
        blockers.append({'code':'REQUIREMENT_MISSING','message':message,'section':path.split('.')[0],
                         'fieldPath':path,'requiredAction':stage})
    for path in p['requiredFields']:
        val = data
        for part in path.split('.'):
            val = val.get(part) if isinstance(val,dict) else None
        if val is None or val == '':
            block(path, 'Required field is missing')
    ops = data['operations']
    if not (ops['deliveryEnabled'] or ops['diningEnabled']):
        block('operations.deliveryEnabled', 'Enable at least one service')
    if not ops['cuisineIds']:
        block('operations.cuisineIds', 'Select at least one cuisine')
    for service in ('delivery','dining'):
        if not ops[service + 'Enabled']:
            continue
        if not any(d.get('enabled') and d.get('slots') for d in ops[service + 'Schedule'].values()):
            block('operations.' + service + 'Schedule', 'Provide operating hours')
        source = 'delivery' if service == 'dining' and ops['useDeliveryMenuForDining'] else service
        if menu_counts.get(source,0) == 0:
            block('operations.' + service + 'MenuAssetIds', 'Import at least one menu item for this service')
    for cat in p['requiredImageCategories']:
        key = cat + ('AssetId' if cat == 'profile' else 'AssetIds')
        if not data['media'].get(key):
            block('media.' + key, 'Required image category is missing')
    docs = data.get('documents',{})
    for kind in p['requiredDocuments']:
        doc = docs.get(kind)
        if not doc or doc['status'] in ('Not provided','Changes requested','Rejected'):
            block('documents.' + kind, 'Provide document evidence')
        if kind == 'FSSAI' and doc and doc.get('expiry','9999-12-31') <= date.today().isoformat():
            block('documents.FSSAI.expiry','FSSAI has expired')
        if not doc or doc['status'] != 'Approved':
            block('documents.' + kind, 'Document requires approval', 'approve')
    contract = data.get('contract')
    if p['contractRequiredForApproval'] and (not contract or contract.get('status') != 'Signed'):
        block('contract', 'Current contract requires validated signature evidence', 'approve')
    can_submit = not any(b['requiredAction']=='submit' for b in blockers)
    can_approve = not blockers
    return {'canSubmit': status in EDITABLE and can_submit,
            'canApprove': status == 'Under review' and can_approve,
            'canActivate': status == 'Approved' and can_approve, 'blockers': blockers}


def progress(data, ready):
    result = []
    for section in ('restaurant','owner','location','media','operations','documents','contract'):
        blockers = [b for b in ready['blockers'] if b['section']==section]
        content = data.get(section)
        started = bool(content) and any(bool(x) for x in content.values()) if isinstance(content,dict) else bool(content)
        state = 'Complete' if not blockers else ('In progress' if started else 'Not started')
        if section == 'documents' and any(d['status'] in ('Rejected','Changes requested') for d in (content or {}).values()):
            state = 'Needs attention'
        result.append({'section':section,'state':state,'blockerCount':len(blockers)})
    return result
