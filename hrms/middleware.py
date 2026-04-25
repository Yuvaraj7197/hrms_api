from django.db import connection
from django.http import JsonResponse
from urllib.parse import urlparse

class TenantMiddleware:

    tenant_map = {
           
            'localhost:4200': 'hrms_local',
            'localhost:4211': 'hrms_local',
            'localhost': 'hrms_local',
            '127.0.0.1': 'hrms_local'
        }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
           

            origin = request.META.get('HTTP_ORIGIN', '') or request.META.get('HTTP_REFERER', '')
            domain = ''
            
            log_request(request)


            # Get the host (domain) of your server
            if not origin :
                print(f'orgin empty=======path={request.path}')
                origin = request.get_host().split(':')[0]  # Get the domain without port
                domain = get_hostname_from_referer(origin)
            else:           
                # Get the domain from the request
                domain = get_hostname_from_referer(origin)

            # check for api version or static/media files
            if "api/version" in request.path or "/media/" in request.path or "/static/" in request.path:
                # Default to hms_saas_uat for shared assets if needed, or just let it pass
                connection.settings_dict['NAME'] = 'hms_saas_uat'
                pass
            # Get the tenant based on the domain
            elif domain in self.tenant_map:
                connection.settings_dict['NAME'] = self.tenant_map[domain]
            else:
                print(f'Origin not found. Access denied, domain={domain}')
                return JsonResponse({"detail":"Access denied"}, status=401)
           
        except Exception as e:
            print(e)
            return JsonResponse({"detail":"Access denied"}, status=401)
        return self.get_response(request)
    
def get_hostname_from_referer(referer_url):
    if referer_url:
        parsed_url = urlparse(referer_url)
        return parsed_url.netloc  # Returns hostname (without scheme)
    return None

def log_request(request):
    try: 
        # Capture request details
        log_data = {
            "client_ip": request.META.get("REMOTE_ADDR", "Unknown"),
            "user_agent": request.META.get("HTTP_USER_AGENT", "Unknown"),
            "method": request.method,
            "path": request.path,
            "auth_token": request.META.get("HTTP_AUTHORIZATION", "None"),
            "origin": request.META.get("HTTP_ORIGIN", "Unknown"),
            "referer": request.META.get("HTTP_REFERER", "Unknown"),
            "content_type": request.content_type,
            "query_params": dict(request.GET),
            "post_data": dict(request.POST),
            "cookies": request.COOKIES,
        }
        print('request details=====================start==========================')
        print(log_data)
        print('request details=====================end==========================')
    except Exception as e:
        print('request details=====================start=error=========================')
        print(log_data)
        print(e)
        print('request details=====================end==========================')



import json
import logging
import time

logger = logging.getLogger(__name__)

class SecurityMonitoringMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start_time = time.time()

        # Capture request details
        log_data = {
            "client_ip": request.META.get("REMOTE_ADDR", "Unknown"),
            "user_agent": request.META.get("HTTP_USER_AGENT", "Unknown"),
            "method": request.method,
            "path": request.path,
            "auth_token": request.META.get("HTTP_AUTHORIZATION", "None"),
            "origin": request.META.get("HTTP_ORIGIN", "Unknown"),
            "referer": request.META.get("HTTP_REFERER", "Unknown"),
            "content_type": request.content_type,
            "query_params": dict(request.GET),
            "post_data": dict(request.POST),
            "cookies": request.COOKIES,
        }

        # Handle JSON body
        if request.method in ["POST", "PUT", "PATCH"]:
            content_type = request.META.get('CONTENT_TYPE', '')
            if 'application/json' in content_type:
                try:
                    log_data["body_data"] = json.loads(request.body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    log_data["body_data"] = {"error": "Invalid or Non-UTF8 JSON"}
            else:
                log_data["body_data"] = {"info": f"Non-JSON body (Content-Type: {content_type})"}

        # Log the request
        logger.error(json.dumps(log_data, default=str))

        # Process request
        response = self.get_response(request)

        # Capture response status and execution time
        log_data["status_code"] = response.status_code
        log_data["execution_time"] = round(time.time() - start_time, 4)

        # Log response
        logger.error(json.dumps(log_data, default=str))

        return response
