#!/usr/bin/env python3

import dns.exception
import dns.resolver
import logging
import os
import requests
import sys
import time

from tld import get_fld

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler(sys.stdout))

if os.environ.get('CF_DEBUG'):
    logger.setLevel(logging.DEBUG)
else:
    logger.setLevel(logging.INFO)

# Initialize empty headers list
CF_HEADERS = []

# Check for API token first
try:
    CF_HEADERS.extend([{
        'Authorization': f'Bearer {api_token}',
        'Content-Type': 'application/json',
    } for api_token in os.environ['CF_API_TOKEN'].split()])
except KeyError:
    # If no API token, try email/key authentication
    try:
        CF_HEADERS.extend([{
            'X-Auth-Email': email,
            'X-Auth-Key': api_key,
            'Content-Type': 'application/json',
        } for email, api_key in zip(os.environ['CF_EMAIL'].split(), os.environ['CF_KEY'].split())])
    except KeyError:
        logger.error(" + Unable to locate Cloudflare credentials in environment!")
        logger.error(" + Please set the CF_API_TOKEN environment variable with your API token.")
        sys.exit(1)

if not CF_HEADERS:
    logger.error(" + No valid Cloudflare credentials found in environment!")
    sys.exit(1)

try:
    dns_servers = os.environ['CF_DNS_SERVERS']
    dns_servers = dns_servers.split()
except KeyError:
    dns_servers = False


def _has_dns_propagated(domain_name, token):
    try:
        if dns_servers:
            custom_resolver = dns.resolver.Resolver()
            custom_resolver.nameservers = dns_servers
            dns_response = custom_resolver.resolve(domain_name, 'TXT')
        else:
            dns_response = dns.resolver.resolve(domain_name, 'TXT')

        for rdata in dns_response:
            if token in [b.decode('utf-8') for b in rdata.strings]:
                return True

    except dns.exception.DNSException as e:
        logger.debug(" + {0}. Retrying query...".format(e))

    return False


# https://api.cloudflare.com/#zone-list-zones
def _get_zone_id(domain):
    tld = get_fld('http://' + domain)
    url = "https://api.cloudflare.com/client/v4/zones?name={0}".format(tld)
    for auth in CF_HEADERS:
        r = requests.get(url, headers=auth)
        r.raise_for_status()
        r = r.json().get('result',())
        if r:
            return auth, r[0]['id']
    if 'CF_API_TOKEN' in os.environ:
        logger.error(f"\033[91mERROR:\033[0m None of the provided API tokens have the required permissions for the domain {tld}")
    else:
        logger.error(f"\033[91mERROR:\033[0m Domain {tld} not found in any Cloudflare account")
    sys.exit(1)

# https://api.cloudflare.com/#dns-records-for-a-zone-dns-record-details
def _get_txt_record_id(auth, zone_id, name, token):
    url = "https://api.cloudflare.com/client/v4/zones/{0}/dns_records?type=TXT&name={1}&content={2}".format(zone_id, name, token)
    r = requests.get(url, headers=auth)
    r.raise_for_status()
    try:
        record_id = r.json()['result'][0]['id']
    except IndexError:
        logger.debug(" + Unable to locate record named {0}".format(name))
        return

    return record_id


# https://api.cloudflare.com/#dns-records-for-a-zone-create-dns-record
def create_txt_record(args):
    domain, challenge, token = args
    logger.debug(' + Creating TXT record: {0} => {1}'.format(domain, token))
    logger.debug(' + Challenge: {0}'.format(challenge))
    auth, zone_id = _get_zone_id(domain)
    name = "{0}.{1}".format('_acme-challenge', domain)

    record_id = _get_txt_record_id(auth, zone_id, name, token)
    if record_id:
        logger.debug(" + TXT record exists, skipping creation.")
        return

    url = "https://api.cloudflare.com/client/v4/zones/{0}/dns_records".format(zone_id)
    payload = {
        'type': 'TXT',
        'name': name,
        'content': token,
        'ttl': 120,
    }
    r = requests.post(url, headers=auth, json=payload)
    r.raise_for_status()
    record_id = r.json()['result']['id']
    logger.debug(" + TXT record created, CFID: {0}".format(record_id))


# https://api.cloudflare.com/#dns-records-for-a-zone-delete-dns-record
def delete_txt_record(args):
    domain, token = args[0], args[2]
    if not domain:
        logger.info(" + http_request() error in letsencrypt.sh?")
        return

    auth, zone_id = _get_zone_id(domain)
    name = "{0}.{1}".format('_acme-challenge', domain)
    record_id = _get_txt_record_id(auth, zone_id, name, token)

    if record_id:
        url = "https://api.cloudflare.com/client/v4/zones/{0}/dns_records/{1}".format(zone_id, record_id)
        r = requests.delete(url, headers=auth)
        r.raise_for_status()
        logger.debug(" + Deleted TXT {0}, CFID {1}".format(name, record_id))
    else:
        logger.debug(" + No TXT {0} with token {1}".format(name, token))


def deploy_cert(args):
    domain, privkey_pem, cert_pem, fullchain_pem, chain_pem, timestamp = args
    logger.debug(' + ssl_certificate: {0}'.format(fullchain_pem))
    logger.debug(' + ssl_certificate_key: {0}'.format(privkey_pem))
    return


def unchanged_cert(args):
    return


def invalid_challenge(args):
    domain, result = args[0], " ".join(args[1:])
    logger.debug(' + invalid_challenge for {0}'.format(domain))
    logger.debug(' + Full error: {0}'.format(result))
    return


def create_all_txt_records(args):
    settle_time = int(os.environ.get('CF_SETTLE_TIME', '10'))
    X = 3
    for i in range(0, len(args), X):
        create_txt_record(args[i:i+X])
    # give it some time (default: 10 seconds) to settle down and avoid nxdomain caching
    logger.info(" + Settling down for {}s...".format(settle_time))
    time.sleep(settle_time)
    for i in range(0, len(args), X):
        domain, token = args[i], args[i+2]
        name = "{0}.{1}".format('_acme-challenge', domain)
        while(_has_dns_propagated(name, token) == False):
            logger.info(" + DNS not propagated, waiting 30s...")
            time.sleep(30)


def delete_all_txt_records(args):
    X = 3
    for i in range(0, len(args), X):
        delete_txt_record(args[i:i+X])

def startup_hook(args):
    if 'CF_API_TOKEN' in os.environ and ('CF_EMAIL' in os.environ or 'CF_KEY' in os.environ):
        print("\033[93m + Warning: Both CF_API_TOKEN and CF_EMAIL/CF_KEY environment variables are set.\n   CF_EMAIL and CF_KEY environment variables are no longer needed for this script.\n   You may consider removing them from your environment.\033[0m")
    elif 'CF_EMAIL' in os.environ or 'CF_KEY' in os.environ:
        print("\033[93m + Using Cloudflare account email/key authentication (CF_EMAIL and CF_KEY environment variables).\n   We are planning to deprecate this authentication method. Please switch to API tokens for enhanced security.\n   See the README for more information.\033[0m")
    return

def exit_hook(args):
    return


def main(argv):
    ops = {
        'deploy_challenge': create_all_txt_records,
        'clean_challenge' : delete_all_txt_records,
        'deploy_cert'     : deploy_cert,
        'unchanged_cert'  : unchanged_cert,
        'invalid_challenge': invalid_challenge,
        'startup_hook': startup_hook,
        'exit_hook': exit_hook
    }
    if argv[0] in ops:
        logger.info(" + Cloudflare hook executing: {0}".format(argv[0]))
        ops[argv[0]](argv[1:])

if __name__ == '__main__':
    main(sys.argv[1:])
