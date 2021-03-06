import hashlib
import itertools
import json
import logging
import os
import re

import six
from six.moves import zip_longest

from fuel_ccp.common import jinja_utils
from fuel_ccp.common import utils
from fuel_ccp import config
from fuel_ccp.config import _yaml
from fuel_ccp import kubernetes
from fuel_ccp import templates
from fuel_ccp.validation import deploy as deploy_validation


CONF = config.CONF

LOG = logging.getLogger(__name__)

YAML_FILE_RE = re.compile(r'\.yaml$')
# This role is added to execute k8s Jobs only on nodes affected by deployment
JOBS_ROLE = '_ccp_jobs'


def _expand_items(service, kind, items):
    def _expand(cmd):
        if cmd.get(kind):
            cmd[kind] = {f: items[f] for f in cmd[kind]}

    for cont in service["containers"]:
        _expand(cont["daemon"])
        for cmd in cont.get("pre", ()):
            _expand(cmd)
        for cmd in cont.get("post", ()):
            _expand(cmd)


def _get_configmaps_version(configmaps, files, configs):
    """Get overall ConfigMaps version

    If any of the ConfigMaps changed, the overall version will be
    changed and deployment will be updated no matter if the deployment spec
    was updated or not.
    """
    versions = ''.join(cm.obj['metadata']['resourceVersion']
                       for cm in configmaps)
    files_hash = _get_service_files_hash(files, configs)

    return versions + files_hash


def _get_service_files_hash(files, configs):
    data = {}
    if files:
        for filename, f in files.items():
            data[filename] = jinja_utils.jinja_render(
                f["content"], configs, [utils.address],
                ignore_undefined=True)
    dump = json.dumps(data, sort_keys=True).encode("utf-8")
    return hashlib.sha1(dump).hexdigest()


def process_files(files, service_dir):
    if not files:
        return
    for filename, f in files.items():
        if CONF.files.get(filename):
            content = CONF.files.get(filename)
        else:
            content = os.path.join(service_dir, "files", f["content"])
        f["content"] = content


def serialize_workflows(workflows):
    for k, v in six.iteritems(workflows):
        workflows[k] = json.dumps(v, sort_keys=True)


def _process_secrets(secrets):
    if secrets:
        for secret in six.itervalues(secrets):
            type = secret.get("type", "Opaque")
            data = secret.get("data", {})
            yield templates.serialize_secret(secret["secret"]["secretName"],
                                             type, data)


def parse_role(component, topology, configmaps, components_map):
    service_dir = component["service_dir"]
    role = component["service_content"]
    component_name = component["component_name"]
    service = role["service"]
    service_name = service["name"]

    if service_name not in topology:
        raise ValueError('The %s is not defined in topology.' % service_name)

    LOG.info("Scheduling service %s deployment", service_name)

    for kind in ["files", "secrets"]:
        _expand_items(service, kind, role.get(kind))

    files_header = service['exports_ctx']['files_header']
    files = role.get("files")
    process_files(files, service_dir)
    files_cm = _create_files_configmap(service_name, files, files_header)
    meta_cm = _create_meta_configmap(service)
    service_configs = utils.get_service_configs(service_name)
    _create_service_configmap(service_name, service_configs)

    yield _process_secrets(role.get("secrets"))

    workflows = _parse_workflows(service)
    if CONF.kubernetes.appcontroller['enabled']:
        yield create_dependencies(workflows, components_map)
    serialize_workflows(workflows)
    workflow_cm = _create_workflow(workflows, service_name)
    configmaps = configmaps + (files_cm, meta_cm, workflow_cm)

    if CONF.action.dry_run:
        cm_version = 'dry-run'
    else:
        rendering_context = utils.get_rendering_config()
        # update with node-related params
        for node_name, node in sorted(CONF.nodes._items()):
            rendering_context._merge(node.get('configs', {}))

        # update with service-related params
        rendering_context._merge(service_configs)
        rendering_context['_current_service'] = service_name

        cm_version = _get_configmaps_version(
            configmaps, files, rendering_context._dict)

    for cont in service["containers"]:
        daemon_cmd = cont["daemon"]
        daemon_cmd["name"] = cont["name"]

        yield _create_pre_jobs(service, cont, component_name, topology)
        yield _create_post_jobs(service, cont, component_name, topology)
        cont['cm_version'] = cm_version

    cont_spec = templates.serialize_daemon_pod_spec(service)
    affinity = templates.serialize_affinity(service, topology)

    replicas = CONF.replicas.get(service_name)
    strategy = {'type': service.get('strategy', 'RollingUpdate')}
    if strategy['type'] == 'RollingUpdate' and (
            service.get("hostNetwork") or service.get("antiAffinity")):
        strategy['rollingUpdate'] = {'maxSurge': 0,
                                     'maxUnavailable': '50%'}
    if service.get("kind") == 'DaemonSet':
        LOG.warning("Deployment is being used instead of DaemonSet to support "
                    "updates")
        if replicas is not None:
            LOG.error("Replicas was specified for %s, but it's implemented "
                      "in DaemonSet-like way and will be deployed on "
                      "all matching nodes (section 'nodes' in config file)",
                      service_name)
            raise RuntimeError("Replicas couldn't be specified for services "
                               "implemented using Kubernetes DaemonSet")
        replicas = len(set(topology[service_name]))
        if strategy['type'] == 'RollingUpdate':
            strategy['rollingUpdate'] = {'maxSurge': 0,
                                         'maxUnavailable': '50%'}
    else:
        replicas = replicas or 1

    annotations = service.get('annotations', {}).get('pod', {})
    same_keywords = set(annotations) & set(affinity)
    if same_keywords:
        raise RuntimeError(
            'Affinity is in conflict with annotations with key: %s'
            .format(same_keywords))
    annotations.update(affinity)

    if service.get("kind") != "StatefulSet":
        obj = templates.serialize_deployment(service_name, cont_spec,
                                             annotations, replicas,
                                             component_name, strategy)
    else:
        obj = templates.serialize_statefulset(service_name, cont_spec,
                                              annotations, replicas,
                                              component_name)
    yield [obj]

    yield _process_ports(service)
    LOG.info("Service %s successfuly scheduled", service_name)


def _parse_workflows(service):
    workflows = {}
    for cont in service["containers"]:
        job_wfs = _create_job_wfs(cont, service["name"])
        workflows.update(job_wfs)

        wf = {}
        _create_pre_commands(wf, cont)
        _create_daemon(wf, cont, service['name'])
        _create_post_commands(wf, cont)
        workflows.update({cont["name"]: {"workflow": wf}})
    return workflows


def _create_job_wfs(container, service_name):
    wfs = {}
    for job in container.get("pre", ()):
        if _is_single_job(job):
            wfs.update(_create_job_wf(job, service_name, container))
    for job in container.get("post", ()):
        if _is_single_job(job):
            wfs.update(_create_job_wf(job, service_name, container, True))
    return wfs


def _fill_cmd(workflow, cmd):
    workflow.update({"command": cmd["command"]})
    if "user" in cmd:
        workflow.update({"user": cmd["user"]})


def _create_workflow(workflow, name):
    configmap_name = "%s-%s" % (name, templates.ROLE_CONFIG)
    template = templates.serialize_configmap(configmap_name, workflow)
    return kubernetes.process_object(template)


def _process_ports(service):
    template_ports = service.get("ports")
    if not template_ports:
        return
    ports = []
    ingress_rules = []
    for port in service["ports"]:
        source_port = int(port.get('cont'))
        node_port = port.get('node')
        port_name = str(source_port)
        if node_port:
            ports.append({"port": source_port, "name": port_name,
                          "node-port": int(node_port)})
        else:
            ports.append({"port": source_port, "name": port_name})

        if CONF.configs.ingress.enabled and port.get("ingress"):
            ingress_host = utils.get_ingress_host(port.get("ingress"))
            if ingress_host:
                ingress_rules.append(templates.serialize_ingress_rule(
                    service["name"], ingress_host, source_port))
    service_template = templates.serialize_service(
        service["name"], ports,
        headless=service.get("kind") == "StatefulSet" or service.get(
            "headless", False),
        annotations=service.get('annotations', {}).get('service'))
    yield service_template

    if ingress_rules:
        ingress_template = templates.serialize_ingress(
            service["name"], ingress_rules)
        yield ingress_template


def _create_pre_commands(workflow, container):
    workflow["pre"] = []
    for cmd in container.get("pre", ()):
        _create_command(workflow["pre"], cmd)


def _create_daemon(workflow, container, service_name):
    workflow["name"] = "%s/%s" % (service_name, container["name"])
    daemon = container["daemon"]
    workflow["dependencies"] = []
    # TODO(sreshetniak): add files from job
    for cmd in container.get("pre", ()):
        if cmd.get("type", "local") == "single":
            workflow["dependencies"].append("%s/%s" % (
                service_name, cmd["name"]))
    workflow["dependencies"].extend(daemon.get("dependencies", ()))
    workflow["daemon"] = {}
    _fill_cmd(workflow["daemon"], daemon)
    _push_files_to_workflow(workflow, daemon.get("files"))
    readiness_cmd = container.get("probes", {}).get("readiness")
    if readiness_cmd:
        workflow["readiness"] = readiness_cmd


def _create_post_commands(workflow, container):
    LOG.debug("Create post jobs")
    workflow["post"] = []
    for cmd in container.get("post", ()):
        _create_command(workflow["post"], cmd)


def _is_single_job(job):
    return job.get("type", "local") == "single"


def _create_pre_jobs(service, container, component_name, topology):
    for job in container.get("pre", ()):
        if _is_single_job(job):
            yield _get_job(service, container, job, component_name, topology)


def _create_post_jobs(service, container, component_name, topology):
    for job in container.get("post", ()):
        if _is_single_job(job):
            yield _get_job(service, container, job, component_name, topology)


def _get_job(service, container, job, component_name, topology):
    if 'topology_key' in job:
        key = job['topology_key']
        if key not in topology:
            raise ValueError('You must specify "%s" in topology to setup '
                             'affinity for "%s" job of "%s" component' %
                             (key, job['name'], component_name))
        affinity = templates.serialize_affinity({"name": key}, topology)
    else:
        affinity = templates.serialize_affinity({"name": JOBS_ROLE}, topology)
    cont_spec = templates.serialize_job_container_spec(container, job)
    pod_spec = templates.serialize_job_pod_spec(service, job, cont_spec,
                                                affinity)
    job_name = "%s-%s" % (service["name"], job["name"])
    job_spec = templates.serialize_job(job_name, pod_spec, component_name,
                                       service["name"])
    return job_spec


def _create_command(workflow, cmd):
    if cmd.get("type", "local") == "local":
        cmd_flow = {}
        _fill_cmd(cmd_flow, cmd)
        workflow.append(cmd_flow)


def _create_job_wf(job, service_name, cont, post=False):
    wrk = {}
    wrk["name"] = "%s/%s" % (service_name, job["name"])
    wrk["dependencies"] = job.get("dependencies", [])
    if post:
        wrk["dependencies"].append("%s/%s" % (service_name, cont["name"]))
    wrk["job"] = {}
    _fill_cmd(wrk["job"], job)
    _push_files_to_workflow(wrk, job.get("files"))
    return {job["name"]: {"workflow": wrk}}


def _push_files_to_workflow(workflow, files):
    if not files:
        return
    workflow["files"] = [{
        "name": filename,
        "path": f["path"],
        "perm": f.get("perm"),
        "user": f.get("user")
    } for filename, f in sorted(files.items())]


def _create_globals_configmap(config):
    data = {templates.GLOBAL_CONFIG: config._json(sort_keys=True)}
    cm = templates.serialize_configmap(templates.GLOBAL_CONFIG, data)
    return kubernetes.process_object(cm)


def _create_globals_secret(conf):
    data = {templates.GLOBAL_SECRET_CONFIG: conf._json(sort_keys=True)}
    secret = templates.serialize_secret(
        templates.GLOBAL_SECRET_CONFIG, data=data)
    return kubernetes.process_object(secret)


def _create_nodes_configmap(nodes):
    nodes_config = utils.get_nodes_config(nodes)
    data = {templates.NODES_CONFIG: nodes_config}
    cm = templates.serialize_configmap(templates.NODES_CONFIG, data)
    return kubernetes.process_object(cm)


def _create_service_configmap(service_name, service_config):
    configmap_name = "%s-%s" % (service_name, templates.SERVICE_CONFIG)
    data = {templates.SERVICE_CONFIG: service_config._json(sort_keys=True)}
    template = templates.serialize_configmap(configmap_name, data)
    return kubernetes.process_object(template)


def get_start_script():
    start_scr_path = os.path.join(CONF.repositories.path,
                                  CONF.repositories.entrypoint_repo_name,
                                  "fuel_ccp_entrypoint",
                                  "start_script.py")
    with open(start_scr_path) as f:
        return f.read()


def create_start_script_configmap():
    data = {
        templates.SCRIPT_CONFIG: get_start_script()
    }
    cm = templates.serialize_configmap(templates.SCRIPT_CONFIG, data)
    return kubernetes.process_object(cm)


def _create_files_configmap(service_name, files, macros_imports):
    configmap_name = "%s-%s" % (service_name, templates.FILES_CONFIG)
    data = {}
    if files:
        for filename, f in files.items():
            with open(f["content"], "r") as f:
                data[filename] = macros_imports + f.read()
    data["placeholder"] = ""
    template = templates.serialize_configmap(configmap_name, data)
    return kubernetes.process_object(template)


def _create_meta_configmap(service):
    configmap_name = "%s-%s" % (service["name"], templates.META_CONFIG)
    data = {
        templates.META_CONFIG: json.dumps(
            {"service-name": service["name"],
             "host-net": service.get("hostNetwork", False)}, sort_keys=True)
    }
    template = templates.serialize_configmap(configmap_name, data)
    return kubernetes.process_object(template)


def _create_exports_configmap(exports_map):
    """Create config map of files from fuel-ccp-repo/exports dirs."""
    exported_files_content = {}
    for key in exports_map:
        exported_files_content[key] = exports_map[key]['body']
    serialized = templates.serialize_configmap(templates.EXPORTS_CONFIG,
                                               exported_files_content)
    return kubernetes.process_object(serialized)


def _make_topology(nodes, roles, replicas):
    failed = False
    if not deploy_validation.validate_nodes_section(nodes, CONF.configs):
        failed = True
    # TODO(sreshetniak): move it to validation
    if not roles:
        LOG.error("Roles section is not specified in configs")
        failed = True
    if failed:
        raise RuntimeError("Failed to create topology for services")

    # Replicas are optional, 1 replica will deployed by default
    replicas = replicas or _yaml.AttrDict()

    # TODO(sreshetniak): add validation
    k8s_nodes = kubernetes.list_k8s_nodes()
    k8s_node_names = kubernetes.get_object_names(k8s_nodes)

    def find_match(glob):
        matcher = re.compile(glob)
        nodes = []
        for node in k8s_node_names:
            match = matcher.match(node)
            if match:
                nodes.append(node)
        return nodes

    roles_to_node = {}
    for node in sorted(nodes):
        matched_nodes = find_match(node)
        for role in nodes[node]["roles"] + [JOBS_ROLE]:
            roles_to_node.setdefault(role, [])
            roles_to_node[role].extend(matched_nodes)
    service_to_node = {}
    for role in sorted(roles):
        if role in roles_to_node:
            for svc in roles[role]:
                service_to_node.setdefault(svc, [])
                service_to_node[svc].extend(roles_to_node[role])
        else:
            LOG.warning("Role '%s' defined, but unused", role)

    replicas = replicas._dict.copy()
    for svc, svc_hosts in six.iteritems(service_to_node):
        svc_replicas = replicas.pop(svc, None)

        if svc_replicas is None:
            continue

        svc_hosts_count = len(svc_hosts)
        if svc_replicas > svc_hosts_count:
            LOG.error("Requested %s replicas for %s while only %s hosts able "
                      "to run that service (%s)", svc_replicas, svc,
                      svc_hosts_count, ", ".join(svc_hosts))
            raise RuntimeError("Replicas doesn't match available hosts.")

    if replicas:
        LOG.error("Replicas defined for unspecified service(s): %s",
                  ", ".join(replicas.keys()))
        raise RuntimeError("Replicas defined for unspecified service(s)")
    service_to_node[JOBS_ROLE] = roles_to_node[JOBS_ROLE]
    return {k: sorted(set(v)) for k, v in service_to_node.items()}


def _create_namespace(configs):
    if CONF.action.dry_run:
        return

    template = templates.serialize_namespace(configs['namespace'])
    kubernetes.process_object(template)


def _create_openrc(config):
    openrc = [
        "export OS_PROJECT_DOMAIN_NAME=default",
        "export OS_USER_DOMAIN_NAME=default",
        "export OS_PROJECT_NAME=%s" % config['openstack']['project_name'],
        "export OS_USERNAME=%s" % config['openstack']['user_name'],
        "export OS_PASSWORD=%s" % config['openstack']['user_password'],
        "export OS_IDENTITY_API_VERSION=3",
        "export OS_AUTH_URL=%s/v3" %
        utils.address({}, 'keystone', config['keystone']['public_port'], True,
                      True)
    ]
    if config['security']['tls']['create_certificates']:
        with open('ca-cert.pem', 'w') as cert_file:
            cert_file.write(config['security']['tls']['ca_cert'])
        file_path = os.path.join(os.getcwd(), "ca-cert.pem")
        openrc.append("export OS_CACERT=%s" % file_path)

    with open('openrc-%s' % config['namespace'], 'w') as openrc_file:
        openrc_file.write("\n".join(openrc))
    LOG.info("Openrc file for this deployment created at %s/openrc-%s",
             os.getcwd(), config['namespace'])


def check_images_change(objects):
    for obj in objects:
        if obj['kind'] not in ('Deployment', 'DaemonSet', 'PetSet'):
            continue
        kube_obj = kubernetes.get_pykube_object_if_exists(obj)
        if kube_obj is None:
            continue
        old_obj = kube_obj.obj
        old_containers = old_obj['spec']['template']['spec']['containers']
        old_images = [c['image'] for c in old_containers]
        new_containers = obj['spec']['template']['spec']['containers']
        new_images = [c['image'] for c in new_containers]
        for old_image, new_image in zip_longest(old_images, new_images):
            if old_image != new_image:
                return old_image, new_image
    return False


def create_upgrade_jobs(component_name, upgrade_data, configmaps, topology,
                        exports_ctx):
    from_version = upgrade_data['_meta']['from']
    to_version = upgrade_data['_meta']['to']
    component = upgrade_data['_meta']['component']
    upgrade_def = component['upgrades']['default']['upgrade']
    files = component['upgrades']['default'].get('files')
    prefix = '{}-{}-{}'.format(upgrade_def['name'], from_version, to_version)

    LOG.info("Scheduling component %s upgrade", component_name)
    for step in upgrade_def['steps']:
        if step.get('files'):
            step['files'] = {f: files[f] for f in step['files']}

    process_files(files, component['service_dir'])
    _create_files_configmap(prefix, files, exports_ctx['files_header'])
    container = {
        "name": prefix,
        "pre": [],
        "daemon": {},
        "image": upgrade_def['image'],
    }
    service = {
        "name": prefix,
        "containers": [container],
        "exports_ctx": exports_ctx,
    }
    _create_meta_configmap(service)
    _create_service_configmap(prefix, _yaml.AttrDict())

    workflows = {prefix: ""}
    jobs = container["pre"]
    last_deps = []

    for step in upgrade_def['steps']:
        step_type = step.get('type', 'single')
        job_name = "{}-{}".format(prefix, step['name'])
        job = {"name": step['name'], "type": "single"}
        for key in ['files', 'volumes', 'topology_key']:
            if step.get(key):
                job[key] = step[key]
        jobs.append(job)
        workflow = {
            'name': job_name,
            'dependencies': last_deps,
        }
        last_deps = [job_name]
        if step_type == 'single':
            workflow['job'] = job = {}
            _fill_cmd(job, step)
            _push_files_to_workflow(workflow, step.get('files'))
        elif step_type == 'rolling-upgrade':
            services = step.get('services')
            if services is None:
                services = [s for s in upgrade_data if s != '_meta']
            workflow['roll'] = roll = []
            for service_name in services:
                roll.extend(upgrade_data[service_name])
        elif step_type == 'kill-services':
            services = step.get('services')
            if services is None:
                services = [s for s in upgrade_data if s != '_meta']
            workflow['kill'] = kill = []
            for service_name in services:
                for object_dict in upgrade_data[service_name]:
                    if object_dict['kind'] == 'Deployment':
                        kill.append(object_dict)
        else:
            raise RuntimeError("Unsupported upgrade step type: %s" % step_type)
        workflows[step['name']] = \
            json.dumps({'workflow': workflow}, sort_keys=True)

    _create_workflow(workflows, prefix)

    job_specs = _create_pre_jobs(service, container, component_name, topology)
    for job_spec in job_specs:
        kubernetes.process_object(job_spec)

    LOG.info("Upgrade of component %s successfuly scheduled", component_name)


def version_diff(from_image, to_image):
    from_tag = from_image.rpartition(':')[-1]
    to_tag = to_image.rpartition(':')[-1]
    return from_tag, to_tag


def _create_registry_secret():
    dockercfg = {
        CONF.registry.address: {
            "username": CONF.registry.username,
            "password": CONF.registry.password
        }
    }
    data = {".dockercfg": json.dumps(dockercfg, sort_keys=True)}
    secret = templates.serialize_secret(
        "registry-key", "kubernetes.io/dockercfg", data)
    kubernetes.process_object(secret)


def _format_dependency(dep, components_map):
    service_name, _, dep_name = dep.partition('/')
    # FIXME in general this is not correct...
    if dep_name in components_map:
        kind = components_map[dep_name]['service_content']['service'].get(
            'kind', "deployment")
        return "%s/%s" % (kind.lower(), service_name)
    return "job/%s-%s" % (service_name, dep_name)


def create_dependencies(workflows, components_map):
    for name, wf in six.iteritems(workflows):
        child = _format_dependency(wf['workflow']['name'], components_map)
        for dep in wf['workflow']['dependencies']:
            parent = _format_dependency(dep, components_map)
            dep_name = "-".join((child.partition("/")[-1],
                                 parent.partition("/")[-1]))[:63].rstrip("-")
            template = templates.serialize_dependency(dep_name, parent, child)
            yield template


def deploy_components(components_map, components):

    topology = _make_topology(CONF.nodes, CONF.roles, CONF.replicas)
    if not components:
        components = set(topology.keys()) & set(components_map.keys())
    else:
        diff = components - set(topology.keys())
        if diff:
            raise ValueError('The next components are not '
                             'defined in topology: %s' % list(diff))

    deploy_validation.validate_requested_components(components, components_map)

    if CONF.action.export_dir:
        os.makedirs(os.path.join(CONF.action.export_dir, 'configmaps'))

    _create_namespace(CONF.configs)
    _create_registry_secret()
    _create_globals_configmap(CONF.configs)
    _create_globals_secret(CONF.secret_configs)
    _create_nodes_configmap(CONF.nodes)
    start_script_cm = create_start_script_configmap()

    # load exported j2 templates, which can be used across all repositories
    exports_map = utils.get_repositories_exports()
    j2_imports_files_header = jinja_utils.generate_jinja_imports(exports_map)

    exports_cm = _create_exports_configmap(exports_map)
    exports_ctx = {'files_header': j2_imports_files_header, 'map': exports_map}

    configmaps = (start_script_cm, exports_cm)

    upgrading_components = {}
    for service_name in components:
        service = components_map[service_name]
        service["service_content"]['service']['exports_ctx'] = exports_ctx
        objects_gen = parse_role(service, topology, configmaps, components_map)
        objects = list(itertools.chain.from_iterable(objects_gen))
        component_name = service['component_name']
        do_upgrade = component_name in upgrading_components
        if not do_upgrade and service['component']['upgrades']:
            res = check_images_change(objects)
            do_upgrade = bool(res)
            if do_upgrade:
                from_image, to_image = res
                from_version, to_version = version_diff(from_image, to_image)
                upgrading_components[component_name] = {
                    '_meta': {
                        'from': from_version,
                        'to': to_version,
                        'component': service['component']
                    },
                }
                LOG.info('Upgrade will be triggered for %s'
                         ' from version %s to version %s because image for %s'
                         ' changed from %s to %s',
                         component_name, from_version, to_version,
                         service_name, from_image, to_image)

        if not do_upgrade:
            for obj in objects:
                kubernetes.process_object(obj)
        else:
            upgrading_components[component_name][service_name] = objects

    for component_name, component_upg in upgrading_components.items():
        create_upgrade_jobs(component_name, component_upg, configmaps,
                            topology, exports_ctx)

    if 'keystone' in components:
        conf = utils.get_rendering_config()
        _create_openrc(conf)
