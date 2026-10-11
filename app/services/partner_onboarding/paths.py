"""Exact method/path matching for the admin API's independent auth protocol."""
import re


def is_onboarding_request(request):
    path=request.url.path.removeprefix('/api/v1')
    if not request.url.path.startswith('/api/v1/'):
        return False
    method=request.method
    exact={
        '/admin/session':{'GET','POST'}, '/admin/logout':{'POST'},
        '/onboarding/policy':{'GET'}, '/catalogs/cuisines':{'GET'}, '/catalogs/locations':{'GET'},
        '/locations/search':{'GET'}, '/locations/reverse':{'GET'}, '/restaurants/duplicates':{'GET'},
        '/menu-imports/template':{'GET'},
    }
    if method in exact.get(path,set()): return True
    if path=='/restaurants' and method in {'GET','POST'}:
        return bool(request.cookies.get('bittu_onboarding_session') or request.headers.get('x-onboarding-tenant'))
    # Same patterns and methods as the router; no broad /restaurants/* bypass.
    tail=path.split('/',3)[3] if path.startswith('/restaurants/') and len(path.split('/',3))==4 else ''
    patterns={
        'GET': r'(?:onboarding|readiness|audit|documents|documents/[^/]+/history|verifications/[^/]+|contracts/current|contracts/[^/]+/signature-requests/[^/]+|uploads/[^/]+|downloads/[^/]+|menu-imports/[^/]+|menu-items)',
        'POST':r'(?:submit|review/start|review/decisions|approve|activate|uploads|uploads/[^/]+/(?:complete|retry)|assets/[^/]+/download|menu-imports/preview|menu-imports/[^/]+/confirm|documents/[^/]+/(?:submit|decisions|verification)|contracts|contracts/[^/]+/(?:download|signed-copy|signature-requests|signed-copy/approve|supersede))',
        'PATCH':r'(?:onboarding|assets/[^/]+|menu-imports/[^/]+|menu-items/[^/]+)',
        'PUT':r'(?:uploads/[^/]+/content|documents/[^/]+)',
        'DELETE':r'(?:assets/[^/]+|menu-items/[^/]+)',
    }
    return bool(tail and method in patterns and re.fullmatch(patterns[method],tail))
