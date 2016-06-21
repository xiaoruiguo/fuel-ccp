import os

from concurrent import futures
import git
from oslo_config import cfg
from oslo_log import log as logging


CONF = cfg.CONF
CONF.import_group('auth', 'microservices.config.auth')
CONF.import_group('repositories', 'microservices.config.repositories')

LOG = logging.getLogger(__name__)


def fetch_repository(repository_name):
    try:
        dest_dir = os.path.join(CONF.repositories.path, repository_name)
        if os.path.isdir(dest_dir):
            LOG.info('%s was already cloned, skipping', repository_name)
            return
        git_url = getattr(CONF.repositories, repository_name.replace('-', '_'))
        git_url = git_url % CONF.auth.gerrit_username
        git.Repo.clone_from(git_url, dest_dir)
        LOG.info('Cloned %s repo', repository_name)
    except (git.exc.GitCommandError,
            git.exc.HookExecutionError,
            git.exc.CheckoutError) as ex:
        # NOTE(sskripnick) multiprocessing.Pool can't handle some git
        # exceptions properly
        raise Exception("%s" % ex)


def fetch_repositories(repository_names=None):
    if repository_names is None:
        repository_names = CONF.repositories.names

    LOG.info('Cloning repositories into %s', CONF.repositories.path)

    with futures.ThreadPoolExecutor(
            max_workers=CONF.repositories.clone_concurrency) as executor:
        future_list = []
        for repository_name in repository_names:
            future_list.append(executor.submit(
                fetch_repository, repository_name
            ))

        errors = 0
        for future in future_list:
            try:
                future.result()
            except Exception as ex:
                LOG.error("Failed to fetch: %s" % ex)
                errors += 1
        if errors:
            raise Exception("Failed to fetch %d repos" % errors)
