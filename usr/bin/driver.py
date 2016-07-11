#!/usr/bin/env python

#------------------------------------------------------------------------------
# Copyright (c) 2016 Coho Data Inc.
#
# The subject matter distributed under this license is or is based on
# information and material generated by Coho Data Inc. It may only be
# acquired, used, modified and distributed under the terms of the Coho
# Data Compute Cluster License v1.0.  Except as permitted in the Coho
# Data Compute Cluster License v1.0, all other rights are reserved in
# any copyright or other similar rights which may exist. Execution of
# software distributed under this Coho Data Compute Cluster License
# v1.0 may cause you to acquire third-party software (as described in
# the accompanying documentation) and you agree (a) to comply with the
# applicable licenses thereunder and (b) that Coho is not responsible
# in any way for your compliance or non-compliance with the applicable
# third-party licenses or the consequences of your being subject to
# said licenses or your compliance or non-compliance.
#------------------------------------------------------------------------------

"""Script to drive the dockman API from the command line"""
from __future__ import print_function

import sys
import time
import urllib2
import socket
import json
import os.path
import base64
from urlparse import urlparse
from urllib import urlencode
from collections import OrderedDict
from functools import partial

# pacify newer ssl libraries
import ssl
setattr(ssl, '_create_default_https_context',
        getattr(ssl, '_create_unverified_context', None))

DEFAULT_USER = 'admin'
DEFAULT_VLANID = 2100
DEFAULT_VLANSUBNET = '172.20.31.0/24'
RESOURCEMANAGER = 'resourcemanager'
NODEMANAGER = 'nodemanager'
HISTORYSERVER = 'historyserver'
#YARN_IMAGE = 'andre/yarn-test'
YARN_IMAGE = 'yarn'
WAIT_POD_INTERVAL = 5
DEPLOY_CONSUL_RETRY = 120
DEPLOY_POD_RETRY = 1200

CONFIG = {}

#------------------------------------------------------------------------------
class HTTPFollowRedirectHandler(urllib2.HTTPRedirectHandler):
    def __init__(self):
        pass

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        data = None
        if req.has_data():
            data = req.get_data()
        r = urllib2.Request(newurl, headers=req.headers, data=data)
        r.get_method = req.get_method
        return r

class HTTPError(Exception):
    """HTTP exception class with better default printout"""
    def __init__(self, errobj, code=None, content=None, url=None):
        super(HTTPError, self).__init__()
        self.code = code or errobj.code
        self.content = content or errobj.read()
        self.url = url or errobj.geturl()

    def __str__(self):
        return "Code (%d) for URL (%s)" % (self.code, self.url)

def doHTTP(url, method, params=None, data=None, timeout=0.0):
    if params:
        url += "?" + urlencode(params)
    url_info = urlparse(url)
    user = get_cfg('user')
    password = get_cfg('password')
    if password is not None:
        b64 = base64.encodestring('%s:%s' % (user, password))[:-1]
    opener = urllib2.build_opener(HTTPFollowRedirectHandler)
    if data:
        post_data = json.dumps(data)
        request = urllib2.Request(url, data=post_data)
    else:
        request = urllib2.Request(url)
    request.add_header('Content-Type', 'application/json')
    if password is not None:
        request.add_header('Authorization', 'Basic %s' % b64)
    request.get_method = lambda: method
    try:
        if timeout > 0.0:
            rep = opener.open(request, timeout=timeout)
        else:
            # None or 0.0 => no timeout
            rep = opener.open(request)

        return rep.read()

    except urllib2.HTTPError as err:
        if err.code == 401:
            print('Connection error: 401 Unauthorized.\n'
                  'Please ensure that the password is correct.')
            sys.exit(1)
        elif err.code == 403:
            print('Connection error: 403 Forbidden.\n'
                  'Please ensure that microservices are enabled.')
            sys.exit(1)
        else:
            raise HTTPError(err)
    except socket.timeout as e:
        print('Connection error: timeout.')
        sys.exit(1)
    except urllib2.URLError as e:
        # Connection timeouts can trigger ENETUNREACH
        print('Connection error: Host unreachable.')
        sys.exit(1)

def doJson(url, method, params=None, data=None, timeout=0.0):
    x = doHTTP(url, method, params=params, data=data, timeout=timeout)
    return json.loads(x) if x else None

def doJsonGetAll(url, params=None, timeout=0.0):
    data = []

    params = params or {}
    res = doJson(url, 'GET', params=params, timeout=timeout)
    if res is None:
        return data
    count = res['meta']['count']

    page_size = 50 # the page size is always 50.
    numpages = (count / page_size) + (1 if (count % page_size) else 0)
    for page in xrange(numpages):
        pageparams = params.copy()
        pageparams.update({'page': page})
        x = doJson(url, 'GET', params=pageparams, timeout=timeout)
        data.extend(x['data'])

    return data

#------------------------------------------------------------------------------
def waitforpods(ns, url, filters, phase, checkips=True):
    # Retrieve pod list and pass filters as query string params
    if get_cfg('verbose') is True:
        print('Waiting for pods...')
    if get_cfg('debug') is True:
        print('{url}/ns/{ns}/pods'.format(url=url, ns=ns))
        if filters is not None:
            print('filters: %s' % filters)
    pods = doJsonGetAll('{url}/ns/{ns}/pods'.format(
        url=url, ns=ns), params= { 'filters' : filters} if filters else {})

    #Ensure a pod exists
    if len(pods) < 1:
        raise Exception('Could not find Pod that matches %s' % filters)

    if get_cfg('debug') is True:
        print(pods)

    # Check pod state to match filters. Raise if invalid.
    for pod in pods:
        labels = pod.get('labels', {} )
        poddesc = 'Pod %s with labels %s' % (pod['name'], str(labels))
        status = pod.get('status', {})
        p = status.get('phase', None)

        # Make failure reason transparent from user for now
        if p is None:
            raise Exception('%s has not been scheduled' % poddesc)
        if p != phase:
            raise Exception('%s is %s, not %s' % (poddesc, str(p), phase) )
        if checkips:
            ips = status.get('podIPs', [])
            if not ips:
                raise Exception('%s does not have an IP' % poddesc)


def retry_func(fn, limit = 10, interval = 1.0 ):
    """ Retry function until completion or limit reached"""

    attempt = 0
    ret = False

    assert(limit is not None)

    while (attempt < limit):
        try:
            fn()
            ret = True
            break
        except Exception as exc:
            attempt += 1
        time.sleep(interval)

    return ret

def is_pod_running(url, tenant, pod_name, filters, phase='Running',
                   waitforIP=True, retrylimit=30):
    """ Verifies a pod is in the RUNNING state """
    isup = partial(waitforpods, tenant, url, filters, phase, waitforIP)
    pod_running = retry_func(isup, limit=retrylimit, interval=WAIT_POD_INTERVAL)
    if not pod_running:
        print("Unable to start %s (%s); timeout after %ss." %
              (pod_name, tenant, str(retrylimit*WAIT_POD_INTERVAL)) )
        print("Exiting...")
        sys.exit(1)
    # Invalidate known state of pods
    CONFIG['pods'] = None

def _url(ssappip):
    baseurl = 'https://%s/api' % ssappip
    return os.path.join(baseurl, 'dockman')

def get_info(data, attrs):
    try:
        for attr in attrs:
            data = data[attr]
    except Exception as exc:
        data = None
    return data

def get_pods(url, tenant):
    """ Retrieve all pods """
    try:
        resp = doJsonGetAll('{url}/ns/{ns}/pods'.format(url=url, ns=tenant))
    except Exception as exc:
        resp = None
    return resp

def get_tenant_name(url):
    try:
        tenants = doJsonGetAll('{url}/tenant'.format(url=url))
    except HTTPError as err:
        if err.code == 404:
            print('Connection error: 404 Not Found.\n'
                  'Please ensure that the Coho Data Management API address'
                  ' is correct.')
            sys.exit(1)

    if len(tenants) > 0:
        return get_info(tenants[0], ['namespace', 'ns'])

    # This should only occur in ciotest
    nss = doJsonGetAll('{url}/ns'.format(url=url))
    if len(nss) > 0:
        return get_info(nss[0], ['ns'])

    return None

def get_tenant(url, tenant):
    """ Retrieve a tenant """
    try:
        resp = doJson('{url}/tenant/{ns}'.format(url=url, ns=tenant) , 'GET')
    except Exception as exc:
        resp = None

    # This should only occur in ciotest
    if resp is None:
        try:
            ns = doJson('{url}/ns/{ns}'.format(url=url, ns=tenant) , 'GET')
            nt = doJson('{url}/network'.format(url=url) , 'GET')
            resp = {
                'namespace': ns,
                'network': nt['data'][0],
            }
        except Exception as exc:
            resp = None

    return resp

# We store everything in a global CONFIG dictionary so that when multiple steps
# are run in the same script invocation, we don't keep fetching the same bits
# of information over and over from the API.
def get_cfg(key):
    value = CONFIG.get(key, None)
    if value is not None:
        return value

    # Fill in the value as necessary
    if key == 'tenant_name':
        url = get_cfg('api_address')
        CONFIG[key] = get_tenant_name(url)
        if CONFIG[key] is None:
            print('No tenant found.  ' +
                  'Please ensure that microservices are enabled.')
            sys.exit(1)

    elif key == 'tenant':
        url = get_cfg('api_address')
        tenant = get_cfg('tenant_name')
        CONFIG[key] = get_tenant(url, tenant)
        if CONFIG[key] is None:
            print('The specified tenant (%s) does not exist' % str(tenant))
            sys.exit(1)

    elif key == 'yarn_image':
        CONFIG[key] = YARN_IMAGE

    elif key == 'vlanid':
        CONFIG[key] = DEFAULT_VLANID

    elif key == 'vlan_subnet':
        CONFIG[key] = DEFAULT_VLANSUBNET

    elif key == 'network':
        url = get_cfg('api_address')
        tenant = get_cfg('tenant')
        attrs = ['network', 'name']
        CONFIG[key] = get_info(tenant, attrs)
        if CONFIG[key] is None:
            print('Network not found for tenant (%s)' % str(tenant))
            sys.exit(1)

    elif key == 'registryip':
        CONFIG[key] = None
        pods = get_cfg('pods')
        for pod in pods:
            if get_info(pod, ['labels', 'name']) == 'docker-registry':
                ips = get_info(pod, ['status', 'podIPs'])
                if len(ips) > 0:
                    CONFIG[key] = get_info(pod, ['status', 'podIPs'])[0]
        if CONFIG[key] is None:
            print('No registry IP address could be found')
            sys.exit(1)

    elif key == 'consulip':
        CONFIG[key] = None
        pods = get_cfg('pods')
        for pod in pods:
            if get_info(pod, ['labels', 'name']) == 'consul':
                ips = get_info(pod, ['status', 'podIPs'])
                if len(ips) > 0:
                    CONFIG[key] = get_info(pod, ['status', 'podIPs'])[0]
        if CONFIG[key] is None:
            print('No consul IP address could be found')
            sys.exit(1)

    elif key == 'pods':
        url = get_cfg('api_address')
        tenant = get_cfg('tenant_name')
        CONFIG[key] = get_pods(url, tenant)

    else:
        return None

    return CONFIG[key]


#------------------------------------------------------------------------------
# From cio/common/dockman.py
def dm_genpodspec(name='', namespace='', containers=None, volumes=None,
               labels=None, networks=None, restart_policy='', **kwargs):
    ''' Generate the Dockman API spec to be used for defining an standalone Pod
    or a PodTemplate '''
    spec = {
        'spec': {
            'on_networks': networks if networks else [],
            'volumes': volumes if volumes else [],
            'containers': containers if containers else [],
        },
        'labels': labels if labels else {},
    }

    # Standalone pods (no podspec)
    if name:
        spec['name'] = name
    if namespace:
        spec['ns'] = namespace
    if restart_policy:
        spec['spec']['restartPolicy'] = restart_policy

    return spec


def dm_genrcspec(name, namespace, replicas, labels, **kwargs):
    ''' Generate the Dockman API spec to be used for defining a replication
    controller '''

    return {
        'name': name,
        'ns': namespace,
        'spec': {
            'replicas': replicas,
            'replicaSelector': labels,
            'podTemplate': dm_genpodspec(labels=labels, **kwargs),
        }
    }



#------------------------------------------------------------------------------
# From cio/tests/microserviceshdfs.py
def getconsulenv(consulip):
    return [
        {
            "name": "CONSUL_IP",
            "value": "%s" % consulip,
        },
        {
            "name": "CONSUL_RETRY_INTERVAL",
            "value": "5",
        }
    ]

def genrcspec(networkname,
              name,
              image,
              command=[],
              volspec = [],
              replicas=1,
              hostname="",
              env=[]):

    volumes = [{
                'name': volname,
                'source': {'cohoEphemeralDisk': {'sizeMB': sizeMB}},
            } for volname, sizeMB, _ in volspec]
    volumeMounts = [{
                'name': volname,
                'mountPath': mountPath,
            } for volname, _, mountPath in volspec]

    return [
        {
            'name': name,
            'namespace': 'namespace1',
            'containers': [
                {
                    'name': 'container',
                    'hostname': hostname,
                    'image': image,
                    'env': env,
                    'command': command,
                    'volumeMounts': volumeMounts,
                }
            ],
            'replicas': replicas,
            'labels': {'name': name},
            'networks': [
                {
                    'addresses': [],
                    'name': networkname,
                }
            ],
            'volumes': volumes,
        }]

def genconsulrcspec(networkname):
    consul_vol = ('consulvolume', 1000000, '/var/lib/consul')
    return genrcspec(networkname,
              name='consul',
              image='registry:5000/coho/consul',
              volspec=[consul_vol],
              command=['agent -bootstrap -server -data-dir=/var/lib/consul -log-level=debug'])

def genresourcemanagerrcspec(registryip, consulip, networkname):
    env=getconsulenv(consulip)
    env.append({"name": 'ROLE',
                "value": RESOURCEMANAGER
               })

    registry = '%s:5000' % registryip
    image = os.path.join(registry, get_cfg('yarn_image'))

    return genrcspec(networkname,
              name=RESOURCEMANAGER,
              hostname=RESOURCEMANAGER,
              image=image,
              command=['/usr/local/bin/cio-hadoop-run'],
              env=env)

def gennodemanagerrcspec(registryip, consulip, networkname, replicas):
    volspec = [('nmdatadir', 3000000, '/mnt')]
    env=getconsulenv(consulip)
    env.append({"name": 'ROLE',
                "value": NODEMANAGER
               })

    registry = '%s:5000' % registryip
    image = os.path.join(registry, get_cfg('yarn_image'))

    return genrcspec(networkname,
              name=NODEMANAGER,
              image=image,
              volspec=volspec,
              replicas=replicas,
              command=['/usr/local/bin/cio-hadoop-run'],
              env=env)

def genhistoryserverrcspec(registryip, consulip, networkname):
    env=getconsulenv(consulip)
    env.append({"name": 'ROLE',
                "value": HISTORYSERVER
               })

    registry = '%s:5000' % registryip
    image = os.path.join(registry, get_cfg('yarn_image'))

    return genrcspec(networkname,
              name=HISTORYSERVER,
              hostname=HISTORYSERVER,
              image=image,
              command=['/usr/local/bin/cio-hadoop-run'],
              env=env)


def deployrcs(url, namespaces, replicationcontrollers, label, retrylimit=30):

    # update the dockman spec so that the replication controllers referenence
    # images stored in the per-tenant registry.

    rcurl = os.path.join(url, 'ns', namespaces[0], 'replicationcontrollers')
    if get_cfg('verbose') is True:
        print('Data:\n%s' % json.dumps(replicationcontrollers, indent=4))
    for rc in replicationcontrollers:
        try:
            resp = doJson(rcurl, 'POST', data=rc)
        except HTTPError as e:
            labels = get_info(rc, ['spec', 'podTemplate', 'labels'])
            poddesc = '%s with labels %s' % (rc['name'], str(labels))
            if e.code == 409:
                print('Pod %s already exists.' % poddesc)
            else:
                print('Error deploying pod %s.' % poddesc)
                sys.exit(1)
        except Exception as e:
            labels = get_info(rc, ['spec', 'podTemplate', 'labels'])
            poddesc = '%s with labels %s' % (rc['name'], str(labels))
            print('Error deploying pod %s.' % poddesc)
            sys.exit(1)

    # Wait for pod to be "Running"
    for rc in replicationcontrollers:
        is_pod_running(url, namespaces[0], rc['name'], 'labels.name:'+label,
                       "Running", False, retrylimit)

#------------------------------------------------------------------------------
def rm_pod(label, context, step):
    baseurl = get_cfg('api_address')
    tenant = get_cfg('tenant_name')
    if tenant is None:
        error = 'No tenant found!'
        print(error)
        context[step + '-error'] = error
        return

    url = os.path.join(baseurl, 'ns', tenant, 'replicationcontrollers', label)
    try:
        if get_cfg('debug') is True:
            print('DELETE %s' % (url))
        resp = doJson(url, 'DELETE')
    except HTTPError as e:
        if e.code == 404:
            error = ('%s not found' % label)
        else:
            error = ('Unable to delete %s (%s)' % (label, str(e)))
        context[step + '-error'] = error
        pass
    except Exception as e:
        error = ('Unable to delete %s (%s)' % (label, str(e)))
        context[step + '-error'] = error
        pass


    label_attrs = ['labels', 'name']
    name_attrs = ['name']
    pods = None
    pods = get_cfg('pods')
    invalidate_pods = False
    for pod in pods:
        if get_info(pod, label_attrs) == label:
            name = get_info(pod, name_attrs)
            url = os.path.join(baseurl, 'ns', tenant, 'pods', name)
            if get_cfg('debug') is True:
                print('DELETE: %s' % (url))
            resp = doJson(url, 'DELETE')
            invalidate_pods = True
    if invalidate_pods is True:
        CONFIG['pods'] = None

#------------------------------------------------------------------------------
# These are used for debugging only.
def mk_tenant(context):
    url = get_cfg('api_address')

    tenant = get_tenant_name(url)
    if tenant is not None:
        print('Tenant %s already exists.' % tenant)
        return

    vlanid = get_cfg('vlanid')
    vlan_subnet = get_cfg('vlan_subnet')
    print('Creating tenant: VLAN-SUBNET=%s' % (vlan_subnet))
    ns = {
            'namespace': {
                'ns': 'namespace1',
            },
            'network': {
                'mode': 'VLAN',
                'vlan': {
                    'vlanid': vlanid,
                    'v4network': { 'subnet': vlan_subnet, },
                }
            }
         }
    doJson('%s/tenant' % url, 'POST', data=ns)

def rm_tenant(context):
    url = get_cfg('api_address')
    tenant = get_cfg('tenant_name')

    print('Removing the tenant %s' % tenant)
    try:
        x = doJson('%s/tenant/%s' % (url, tenant), 'DELETE')
        print('Finished removing tenant')
    except HTTPError as e:
        if e.code == 404:
            print('tenant %s not found' % tenant)
        else:
            raise e

    CONFIG['tenant_name'] = None
    CONFIG['tenant'] = None

def mk_images(context):
    registryip = get_cfg('registryip')
    tag = get_cfg('tag')
    if tag is not '':
        tag = ':' + tag

    print('# Run the following commands from a machine with access to the'
          ' tenant network.')

    registry_url = '%s' % (get_cfg('registry_url'))
    src = os.path.join(registry_url, get_cfg('yarn_image'))
    src += tag
    print('docker pull %s' % src)

    registry = '%s:5000' % registryip
    dest = os.path.join(registry, get_cfg('yarn_image'))
    dest += tag
    print('docker tag %s %s' % (src, dest))

    print('docker push %s' % dest)

def rm_images(context):
    pass

#------------------------------------------------------------------------------
def mk_consul(context):
    url = get_cfg('api_address')
    tenant = get_cfg('tenant_name')
    namespaces = [tenant]
    network = get_cfg('network')

    replicationcontrollers = []
    for rc in genconsulrcspec(networkname=network):
        replicationcontrollers.append(dm_genrcspec(**rc))

    deployrcs(url, namespaces, replicationcontrollers, 'consul',
              retrylimit=DEPLOY_CONSUL_RETRY)

def rm_consul(context):
    rm_pod('consul', context, 'rm-consul')

def mk_rm(context):
    print('Deploying resource manager.')

    url = get_cfg('api_address')
    tenant = get_cfg('tenant_name')
    if tenant is None:
        print('No tenant found!')
        return

    namespaces = [tenant]
    network = get_cfg('network')
    registryip = get_cfg('registryip')
    consulip = get_cfg('consulip')

    replicationcontrollers = []

    for rc in genresourcemanagerrcspec(registryip=registryip, consulip=consulip, networkname=network):
        replicationcontrollers.append(dm_genrcspec(**rc))

    deployrcs(url, namespaces, replicationcontrollers, RESOURCEMANAGER,
              retrylimit=DEPLOY_POD_RETRY)

def rm_rm(context):
    rm_pod(RESOURCEMANAGER, context, 'rm-rm')

def mk_nm(context):
    print('Deploying node manager.')

    url = get_cfg('api_address')
    tenant = get_cfg('tenant_name')
    if tenant is None:
        print('No tenant found!')
        return

    namespaces = [tenant]
    network = get_cfg('network')
    registryip = get_cfg('registryip')
    consulip = get_cfg('consulip')
    replicas = get_cfg('instances')
    volspec = [('nmdatadir', 3000000, '/mnt')]

    replicationcontrollers = []

    for rc in gennodemanagerrcspec(registryip=registryip, consulip=consulip, networkname=network, replicas=replicas):
        replicationcontrollers.append(dm_genrcspec(**rc))
    deployrcs(url, namespaces, replicationcontrollers, NODEMANAGER,
              retrylimit=DEPLOY_POD_RETRY)

def rm_nm(context):
    rm_pod(NODEMANAGER, context, 'rm-nm')

def mk_hs(context):
    print('Deploying history server.')

    url = get_cfg('api_address')
    tenant = get_cfg('tenant_name')
    if tenant is None:
        print('No tenant found!')
        return

    namespaces = [tenant]
    network = get_cfg('network')
    registryip = get_cfg('registryip')
    consulip = get_cfg('consulip')

    replicationcontrollers = []

    for rc in genhistoryserverrcspec(registryip=registryip, consulip=consulip, networkname=network):
        replicationcontrollers.append(dm_genrcspec(**rc))
    deployrcs(url, namespaces, replicationcontrollers, HISTORYSERVER,
              retrylimit=DEPLOY_POD_RETRY)

def rm_hs(context):
    rm_pod(HISTORYSERVER, context, 'rm-hs')

def get_rm(context):
    label_attrs = ['labels', 'name']
    name_attrs = ['name']
    pods = get_cfg('pods')
    ip_attrs = ['status', 'podIPs']
    found = False
    for pod in pods:
        if get_info(pod, label_attrs) == RESOURCEMANAGER:
            print('Resource manager: %s' % (get_info(pod, ip_attrs)[0]))
            found = True
    if found is False:
        print('No resource manager found.  Is compute cluster deployed?')

#------------------------------------------------------------------------------
MANUAL_ACTIONS = OrderedDict([
                        ('mk-tenant', mk_tenant),
                        ('mk-images', mk_images),
                        ('rm-images', rm_images),
                        ('rm-tenant', rm_tenant),
                        ])

MK_ACTIONS = OrderedDict([
                        ('mk-consul', mk_consul),
                        ('mk-rm', mk_rm),
                        ('mk-hs', mk_hs),
                        ('mk-nm', mk_nm),
                        ('get-rm', get_rm),
                        ])

RM_ACTIONS = OrderedDict([
                        ('rm-nm', rm_nm),
                        ('rm-hs', rm_hs),
                        ('rm-rm', rm_rm),
                        ('rm-consul', rm_consul),
                        ])

def argparser():
    from argparse import ArgumentParser, SUPPRESS
    p = ArgumentParser(description='COHO CDH Hadoop compute cluster '
                                    'management script.')
    p.add_argument('-i', '--yarn_image', type=str, help=SUPPRESS)
    p.add_argument('-p', '--password', type=str,
                   help='Management UI admin password')
    p.add_argument('-d', '--debug', action='store_true', help=SUPPRESS)
    p.add_argument('-v', '--verbose', action='store_true', help=SUPPRESS)
    p.add_argument('api_address', type=str,
                   help='IP address of the Coho Data Management API')
    sp = p.add_subparsers(metavar='command',
                          dest='command',
                          help='One of: create, show, delete')

    pc = sp.add_parser('create', help='Create compute cluster')
    pc.set_defaults(command='create')
    pc.add_argument('instances', type=int,
                    help='Number of Node Manager instances to deploy')

    ps = sp.add_parser('show',   help='Show compute cluster')
    ps.set_defaults(command='show')
    pd = sp.add_parser('delete', help='Destry compute cluster')
    pd.set_defaults(command='delete')

    pm = sp.add_parser('manual')
    pm.set_defaults(command='manual')
    pm.add_argument('instances', type=int,
                    help='Number of Node Manager instances to deploy')
    pm.add_argument('registry_url', type=str, metavar='registry_url',
                    help='URL of the Coho Data CDH Docker image')
    pm.add_argument('-t', '--tag', default='', help=SUPPRESS)
    pm.add_argument('-s', '--steps', type=str, nargs='*',
                    default=[], help=SUPPRESS)
    return p

def usage(parser):
    parser.print_help()
    sys.exit(1)

if __name__ == '__main__':
    _p = argparser()
    _args = vars(_p.parse_args())

    CONFIG['verbose']       = False
    CONFIG['api_address']   = _url(_args.get('api_address', ''))
    CONFIG['vlanid']        = DEFAULT_VLANID
    CONFIG['vlan_subnet']   = DEFAULT_VLANSUBNET
    CONFIG['tag']           = _args.get('tag', '')
    CONFIG['debug']         = _args.get('debug', False)
    CONFIG['verbose']       = _args.get('verbose', False)
    CONFIG['user']          = DEFAULT_USER
    CONFIG['password']      = _args.get('password', None)
    CONFIG['yarn_image']    = _args.get('yarn_image', None)
    command = _args.get('command', '')

    steps = []
    if command == 'create':
        CONFIG['instances'] = _args.get('instances', 1)
        steps = list(MK_ACTIONS)

    elif command == 'show':
        steps = ['get-rm']

    elif command == 'delete':
        steps = list(RM_ACTIONS)

    elif command == 'manual':
        CONFIG['verbose']   = True
        CONFIG['instances'] = _args.get('instances', 1)
        CONFIG['registry_url'] = _args.get('registry_url', '')
        steps = _args.get('steps', [])

    if CONFIG['verbose'] is True:
        print(steps)
    ACTIONS = dict(MANUAL_ACTIONS)
    ACTIONS.update(MK_ACTIONS)
    ACTIONS.update(RM_ACTIONS)

    context = {}
    for step in steps:
        if CONFIG['verbose'] is True:
            print('----------------------------------------------------------')
            print('>>> Starting %s' % step)
        if not ACTIONS.has_key(step):
            print('Invalid step: %s!' % step)
            sys.exit(1)
        ACTIONS[step](context)
        if CONFIG['verbose'] is True:
            print('<<< Completed %s' % step)

    if command == 'create':
        print('Success: compute cluster created.')

    elif command == 'delete':
        errors = ''
        for step in steps:
            error = context.get(step + '-error', None)
            if error is not None:
                errors += '\n  ' + error
        if errors is not '':
            print('Errors encountered:' + errors)
            print('Success: all compute cluster containers removed.')
        else:
            print('Success: compute cluster deleted.')

    elif command == 'manual':
        print('Success: %s' % str(steps))

