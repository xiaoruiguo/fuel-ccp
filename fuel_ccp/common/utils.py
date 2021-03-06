import copy
import itertools
import logging
import os
import pkg_resources

import jinja2
import yaml

import fuel_ccp
from fuel_ccp.common import jinja_utils
from fuel_ccp import config
from fuel_ccp import kubernetes


CONF = config.CONF

LOG = logging.getLogger(__name__)


def get_ingress_host(ingress_name):
    return '.'.join((ingress_name, CONF.configs.ingress.domain))


def get_ingress_domains(components=None):
    components_map = get_deploy_components_info()
    components = components or components_map.keys()
    domains = []
    for component in components:
        service = components_map[component]["service_content"]["service"]
        for port in service.get("ports", []):
            if port.get("ingress"):
                domains.append(get_ingress_host(port.get("ingress")))
    return domains


def get_resource_path(path):
    return pkg_resources.resource_filename(fuel_ccp.version_info.package, path)


def get_repositories_paths():
    """Get repositories paths.

    :returns: list -- list of full repositories paths
    """
    paths = []
    for repo in CONF.repositories.repos:
        paths.append(os.path.join(CONF.repositories.path, repo["name"]))
    return paths


def get_config_paths():
    paths = []
    # Order does matter. At first we add global defaults.
    for conf_path in ("resources/defaults.yaml", "resources/globals.yaml"):
        paths.append(get_resource_path(conf_path))

    # After we add component defaults.
    for repo in get_repositories_paths():
        paths.append(os.path.join(repo, "service", "files", "defaults.yaml"))

    return paths


@jinja2.contextfunction
def address(ctx, service, port=None, external=False, with_scheme=False):
    addr = None
    service_name = service.split('-')[0]
    enable_tls = CONF.configs.get(service_name, {}).get(
        'tls', {}).get('enabled')

    if enable_tls:
        scheme = 'https'
    else:
        scheme = 'http'

    if external:
        if not port:
            raise RuntimeError('Port config is required for external address')
        if CONF.configs.ingress.enabled and port.get('ingress'):
            scheme = 'https'
            addr = "%s:%s" % (get_ingress_host(port['ingress']),
                              CONF.configs.ingress.port)
        elif port.get('node'):
            addr = '%s:%s' % (CONF.configs.k8s_external_ip, port['node'])

    current_service = ctx.get('_current_service')
    if current_service:
        current_service_def = CONF.services.get(current_service, {}).get(
            'service_def')
        if current_service_def == service:
            service = current_service
        else:
            service = CONF.services.get(current_service, {}).get(
                'mapping', {}).get(service) or service
    if addr is None:
        addr = '.'.join((service, CONF.kubernetes.namespace, 'svc',
                         CONF.kubernetes.cluster_domain))
        if port:
            addr = '%s:%s' % (addr, port['cont'])

    if with_scheme:
        addr = "%s://%s" % (scheme, addr)

    return addr


def get_repositories_exports():
    """Load shared templates from ./exports dirs of the repositories. """
    exports = dict()
    for repo in get_repositories_paths():
        exports_dir = os.path.join(repo, 'exports')
        if os.path.exists(exports_dir) and os.path.isdir(exports_dir):
            for export_file in os.listdir(exports_dir):
                # Due to k8s keys constraints we need to remove non-alpha
                cm_key = ''.join([c for c in export_file if c.isalpha()])
                path = os.path.join(exports_dir, export_file)
                LOG.debug('Found shared jinja template file %s', path)
                if cm_key not in exports:
                    exports[cm_key] = {'name': export_file, 'body': []}
                with open(path) as f:
                    exports[cm_key]['body'].append(f.read())
    for cm_key, cm in exports.items():
        # Merge files with the same name
        cm['body'] = '\n'.join(sorted(cm['body']))
    return exports


def get_component_name_from_repo_path(path):
    REPO_NAME_PREFIX = "fuel-ccp-"
    name = os.path.basename(path)
    if name.startswith(REPO_NAME_PREFIX):
        name = name[len(REPO_NAME_PREFIX):]
    return name


def get_service_definitions_map():
    """Maps each service definition to its custom services"""
    s_d_map = {}
    for service_name, value in CONF.services._items():
        s_d_map.setdefault(value['service_def'], [])
        s_d_map[value['service_def']].append(service_name)
    return s_d_map


def extend_dependency(dep, deps_map, services_map, service_mapping):
    """Extends dependencies with service prefix"""
    dep_name = dep.split(':')[0]
    if dep_name not in deps_map:
        # dependency is not a container or job
        # checking service mapping first
        if dep_name in service_mapping:
            dep_name = service_mapping[dep_name]
            service_ref = services_map[dep_name]
        elif dep_name in services_map:
            service_ref = services_map[dep_name]
        else:
            raise RuntimeError('Dependency "%s" not found' % dep_name)
        # adjust deps with container names of the service
        return ["%s/%s" % (dep_name, cnt['name']) for cnt in service_ref[
            'service_content']['service']['containers']]

    dep_service_def = deps_map[dep_name]
    dep_service_name = service_mapping.get(
        dep_service_def) or dep_service_def
    return ["%s/%s" % (dep_service_name, dep)]


def process_dependencies(service, deps_map, services_map):
    service_name = service['service_content']['service']['name']
    service_mapping = CONF.services.get(service_name, {}).get('mapping', {})
    containers = service['service_content']['service']['containers']
    for cont in containers:
        for cmd in itertools.chain(
                cont.get('pre', []), [cont.get('daemon', [])],
                cont.get('post', [])):
            if cmd.get('dependencies'):
                new_deps = []
                for dep in cmd['dependencies']:
                    new_deps.extend(extend_dependency(
                        dep, deps_map, services_map, service_mapping))
                cmd['dependencies'] = new_deps


def extend_with_service_configs(service_name, config):
    service = CONF.services.get(service_name, {})
    config._merge(service.get('configs', {}))
    service_mapping = service.get('mapping')
    if service_mapping:
        for _, target_service in service_mapping._items():
            extend_with_service_configs(target_service, config)


def get_deploy_components_info():
    rendering_context = get_rendering_config()
    service_definitions_map = get_service_definitions_map()
    services_map = {}
    custom_services_map = {}

    for repo in get_repositories_paths():
        service_dir = os.path.join(repo, "service")
        if not os.path.isdir(service_dir):
            continue
        component_name = get_component_name_from_repo_path(repo)

        component = {
            "name": component_name,
            "upgrades": {},
            "service_dir": service_dir,
        }

        upgrade_dir = os.path.join(service_dir, "upgrade")
        if os.path.isdir(upgrade_dir):
            for upgrade_fname in os.listdir(upgrade_dir):
                if not upgrade_fname.endswith('.yaml'):
                    continue
                LOG.debug("Loading upgrade definition: %s", upgrade_fname)
                with open(os.path.join(upgrade_dir, upgrade_fname)) as f:
                    upgrade_def = yaml.load(f)
                key = upgrade_fname[:-len('.yaml')]
                component['upgrades'][key] = upgrade_def

        for service_file in os.listdir(service_dir):
            if service_file.endswith('.yaml'):
                LOG.debug("Rendering service definition: %s", service_file)
                content = jinja_utils.jinja_render(
                    os.path.join(service_dir, service_file),
                    rendering_context._dict, functions=[address]
                )
                LOG.debug("Parse service definition: %s", service_file)
                service_definition = yaml.load(content)
                service_name = service_definition['service']['name']
                services_map[service_name] = {
                    'component': component,
                    'component_name': component_name,
                    'service_dir': service_dir,
                    'service_content': service_definition
                }
                for svc in service_definitions_map.get(service_name, ()):
                    LOG.debug("Rendering service definition: %s for '%s' "
                              "service", service_file, svc)
                    context = copy.deepcopy(rendering_context)
                    context['_current_service'] = svc
                    extend_with_service_configs(svc, context)
                    content = jinja_utils.jinja_render(
                        os.path.join(service_dir, service_file),
                        context._dict, functions=[address]
                    )
                    LOG.debug("Parse service definition: %s for '%s' "
                              "service", service_file, svc)
                    service_definition = yaml.load(content)
                    service_definition['service']['name'] = svc
                    custom_services_map[svc] = {
                        'component': component,
                        'component_name': component_name,
                        'service_dir': service_dir,
                        'service_content': service_definition
                    }

    deps_map = get_dependencies_map(services_map)
    services_map.update(custom_services_map)
    for svc_name, svc in services_map.items():
        process_dependencies(svc, deps_map, services_map)

    return services_map


def get_dependencies_map(services_map):
    """Maps each container and job to its service"""
    deps_map = {}
    for service_name, service in services_map.items():
        containers = service['service_content']['service']['containers']
        for cont in containers:
            deps_map[cont['name']] = service_name
            for job in itertools.chain(cont.get('pre', []),
                                       cont.get('post', [])):
                if job.get('type') == 'single':
                    deps_map[job['name']] = service_name
    return deps_map


def get_deployed_components():
    """Returns set of deployed components."""
    deployed_deployments = kubernetes.list_cluster_deployments()
    deployed_statefulsets = kubernetes.list_cluster_statefulsets()
    deployed_components = set(kubernetes.get_object_names(
        itertools.chain(deployed_deployments, deployed_statefulsets))
    )
    return deployed_components


def get_nodes_config(nodes):
    nodes_config = config._yaml.AttrDict()
    for node in sorted(nodes):
        if 'configs' in nodes[node]:
            nodes_config[node] = nodes[node]['configs']
    return nodes_config._json(sort_keys=True)


def get_rendering_config():
    conf = copy.deepcopy(CONF.configs)
    conf._merge(CONF.secret_configs)
    return conf


def get_service_configs(service_name):
    service_config = config._yaml.AttrDict()
    extend_with_service_configs(service_name, service_config)
    return service_config
