# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Copy files (from one dataset to another)"""

__docformat__ = 'restructuredtext'


import logging
import os.path as op
from shutil import copyfile
import sys

from datalad.interface.base import Interface
from datalad.interface.utils import eval_results
from datalad.interface.base import build_doc
from datalad.support.constraints import (
    EnsureStr,
    EnsureNone,
)
from datalad.support.param import Parameter
from datalad.distribution.dataset import (
    Dataset,
    require_dataset,
)
from datalad.support.annexrepo import AnnexRepo
from datalad.utils import (
    assure_list,
    get_dataset_root,
    Path,
)

from datalad.distribution.dataset import (
    EnsureDataset,
    datasetmethod,
    resolve_path,
)

lgr = logging.getLogger('datalad.local.copyfile')


@build_doc
class CopyFile(Interface):
    """
    """
    _params_ = dict(
        dataset=Parameter(
            # not really needed on the cmdline, but for PY to resolve relative
            # paths
            args=("-d", "--dataset"),
            doc="""target dataset to save copied files into. This may be a
            superdataset containing the actual destination dataset. In this case,
            any changes will be save up to this dataset.[PY: This dataset is also
            the reference for any relative paths. PY].""",
            constraints=EnsureDataset() | EnsureNone()),
        path=Parameter(
            args=("path",),
            metavar='PATH',
            doc="""paths to copy (and possibly a target path to copy to).""",
            nargs='+',
            constraints=EnsureStr() | EnsureNone()),
        recursive=Parameter(
            args=("--recursive", "-r",),
            action='store_true',
            doc="""copy directories recursively"""),
        target_dir=Parameter(
            args=('--target-directory', '-t'),
            metavar='DIRECTORY',
            doc="""copy all PATH arguments into DIRECTORY""",
            constraints=EnsureStr() | EnsureNone()),
        specs_from=Parameter(
            args=('--specs-from',),
            metavar='SOURCE',
            doc="""read list of source and destination path names from a given
            file, or stdin (with '-'). Each line defines either a source
            path, or a source/destination path pair (separated by a null byte
            character).[PY:  Alternatively, a list of 2-tuples with
            source/destination pairs can be given provided. PY]."""),
    )

    @staticmethod
    @datasetmethod(name='copyfile')
    @eval_results
    def __call__(
            path,
            dataset=None,
            recursive=False,
            # TODO needs message
            target_dir=None,
            specs_from=None):
        # Concept
        #
        # Loosely model after the POSIX cp command
        #
        # 1. Determine the target of the copy operation, and its associated
        #    dataset
        #
        # 2. for each source: determine source dataset, query for metadata, put
        #    into target dataset
        #
        # Instead of sifting and sorting through input args, process them one
        # by one sequentially. Utilize lookup caching to make things faster,
        # instead of making the procedure itself more complicated.

        # turn into list of absolute paths
        paths = [resolve_path(p, dataset) for p in assure_list(path)]

        # determine the destination path
        if target_dir:
            target_dir = target = resolve_path(target_dir, dataset)
        else:
            # it must be the last item in the path list
            if len(paths) < 2 and not specs_from:
                raise ValueError("No target directory was given to `copy`.")
            target = paths.pop(-1)
            # target could be a directory, or even an individual file
            if len(paths) > 1:
                # must be a directory
                target_dir = target
                if specs_from:
                    raise ValueError(
                        "Multiple source paths AND a files-from specified, "
                        "this is not supported.")
            else:
                # single source, target be an explicit destination filename
                target_dir = target if target.is_dir() else target.parent

        res_kwargs = dict(
            action='copy',
            logger=lgr,
        )

        target_ds = get_dataset_root(target_dir)
        if not target_ds:
            yield dict(
                path=str(target),
                status='error',
                message='copy destination not within a dataset',
                **res_kwargs
            )
            return

        target_ds = Dataset(target_ds)

        if dataset:
            ds = require_dataset(dataset, check_installed=True,
                                 purpose='copying into')
            if not (ds.pathobj == target_ds.pathobj
                    or ds.pathobj in target_ds.pathobj.parents):
                yield dict(
                    path=ds.path,
                    status='error',
                    message=(
                        'reference dataset does not contain '
                        'destination dataset: %s',
                        target_ds),
                    **res_kwargs
                )
                return
        else:
            ds = None

        lgr.debug('Attempt to copy files into %s', target_ds)

        # lookup cache for dir to repo mappings, and as a DB for cleaning
        # things up
        repo_cache = {}
        # which paths to pass on to save
        to_save = []
        try:
            for src_path, dest_path in _yield_specs(
                    specs_from if specs_from else paths):
                src_path = Path(src_path)
                dest_path = target_dir if dest_path is None else Path(dest_path)
                if not recursive and src_path.is_dir():
                    yield dict(
                        path=str(src_path),
                        status='impossible',
                        message='recursion not enabled, omitting directory',
                        **res_kwargs
                    )
                    continue

                for src_file, dest_file in _yield_src_dest_filepaths(
                        src_path, dest_path):
                    for res in _copy_file(src_file, dest_file, cache=repo_cache):
                        yield dict(
                            res,
                            **res_kwargs
                        )
                        if res.get('status', None) == 'ok':
                            to_save.append(res['destination'])
        finally:
            # cleanup time
            # TODO this could also be the place to stop lingering batch processes
            done = set()
            for _, repo_rec in repo_cache.items():
                repo = repo_rec['repo']
                if not repo or repo.pathobj in done:
                    continue
                tmp = repo_rec.get('tmp', None)
                if tmp:
                    try:
                        tmp.rmdir()
                    except OSError as e:
                        lgr.warning(
                            'Failed to clean up temporary directory: %s', e)
                done.add(repo.pathobj)

        if not to_save:
            # nothing left to do
            return

        yield from (ds if ds else target_ds).save(
            path=to_save,
            # we provide an explicit file list
            recursive=False,
        )


def _yield_specs(specs):
    if specs == '-':
        iter = sys.stdin
    elif isinstance(specs, (list, tuple)):
        iter = specs
    else:
        iter = specs.open('r')

    for spec in iter:
        if isinstance(spec, (list, tuple)):
            src = spec[0]
            dest = spec[1]
        elif isinstance(spec, Path):
            src = spec
            dest = None
        else:
            # deal with trailing newlines and such
            spec = spec.rstrip()
            spec = spec.split('\0')
            src = spec[0]
            dest = None if len(spec) == 1 else spec[1]
        yield src, dest


def _yield_src_dest_filepaths(src, dest, src_base=None):
    """Yield src/dest path pairs

    Parameters
    ----------

    src : Path
      Source file or directory. If a directory, yields files from this
      directory recursively.
    dest : Path
      Destination path. Either a complete file path, or a base directory
      (see `src_base`).
    src_base : Path
      If given, the destination path will be `dest`/`src.relative_to(src_base)`

    Yields
    ------
    src, dest
      Path instances
    """
    if src.is_dir():
        for p in src.iterdir():
            yield from _yield_src_dest_filepaths(p, dest, src_base)
    else:
        # reflect src hierarchy if dest is a directory, otherwise
        yield src, dest / (src.relative_to(src_base) if src_base else src.name)


def _get_repo_record(fpath, cache):
    fdir = fpath.parent
    # get the repository, if there is any.
    # `src_repo` will be None, if there is none, and can serve as a flag
    # for further processing
    repo_rec = cache.get(fdir, None)
    if repo_rec is None:
        repo_root = get_dataset_root(fdir)
        repo_rec = dict(
            repo=None if repo_root is None else Dataset(repo_root).repo,
            # this is different from repo.pathobj which resolves symlinks
            repo_root=Path(repo_root) if repo_root else None)
        cache[fdir] = repo_rec
    return repo_rec


def _copy_file(src, dest, cache):
    str_src = str(src)
    str_dest = str(dest)
    if not op.lexists(str_src):
        print(repr(str_src))
        yield dict(
            path=str_src,
            status='impossible',
            message='no such file or directory',
        )
        return

    # at this point we know that there is something at `src`,
    # and it must be a file
    # get the source repository, if there is any.
    # `src_repo` will be None, if there is none, and can serve as a flag
    # for further processing
    src_repo_rec = _get_repo_record(src, cache)
    src_repo = src_repo_rec['repo']
    # same for the destination repo
    dest_repo_rec = _get_repo_record(dest, cache)
    dest_repo = dest_repo_rec['repo']

    # whenever there is no annex (remember an AnnexRepo is also a GitRepo)
    if src_repo is None or not isinstance(src_repo, AnnexRepo):
        # so the best thing we can do is to copy the actual file into the
        # worktree of the destination dataset.
        # we will not care about unlocking or anything like that we just
        # replace whatever is at `dest`, save() must handle the rest.
        # we are not following symlinks, they cannot be annex pointers
        lgr.info('Copying file from no or non-annex dataset: %s', src)
        _replace_file(str_src, dest, str_dest, follow_symlinks=False)
        yield dict(
            path=str_src,
            destination=str_dest,
            status='ok',
        )
        # TODO if we later deal with metadata, we might want to consider
        # going further
        return

    # now we know that we are copying from an AnnexRepo dataset
    # look for URLs on record
    rpath = str(src.relative_to(src_repo_rec['repo_root']))

    # pull what we know about this file from the source repo
    finfo = src_repo.get_content_annexinfo(
        paths=[rpath],
        # a simple `exists()` will not be enough (pointer files, etc...)
        eval_availability=True,
        # if it truely is a symlink9not just an annex pointer, we would not
        # want to resolve it
        eval_file_type=True,
    ).popitem()[1]
    if 'key' not in finfo or not isinstance(dest_repo, AnnexRepo):
        lgr.info(
            'Copying non-annexed file or copy into non-annex dataset: %s -> %s',
            src, dest_repo)
        # the best thing we can do when the target isn't an annex repo
        # or the source is not an annexed files is to copy the file,
        # but only if we have it
        # (make default True, because a file in Git doesn't have this property)
        if not finfo.get('has_content', True):
            yield dict(
                path=str_src,
                status='impossible',
                message='file has no content available',
            )
            return
        # follow symlinks to pull content from annex
        _replace_file(str_src, dest, str_dest,
                      follow_symlinks=finfo.get('type', 'file') == 'file')
        yield dict(
            path=str_src,
            destination=str_dest,
            status='ok',
        )
        return

    # at this point we are copying an annexed file into an annex repo
    src_key = finfo['key']

    # make an attempt to compute a key in the target repo, this will hopefully
    # become more relevant once "salted backends" are possible
    # https://github.com/datalad/datalad/issues/3357 that could prevent
    # information leakage across datasets
    if finfo.get('has_content', True):
        dest_key = dest_repo.call_git_oneline(['annex', 'calckey', str_src])
    else:
        lgr.warning(
            'File content not available, forced to reuse previous annex key: %s',
            str_src)
        dest_key = src_key

    dest_repo._run_annex_command_json(
        'fromkey',
        # we use force, because in all likelihood there is no content for this key
        # yet
        opts=[dest_key, str_dest, '--force'],
    )
    if 'objloc' in finfo:
        # we have the chance to place the actual content into the target annex
        # put in a tmp location, git-annex will move from there
        tmploc = dest_repo_rec.get('tmp', None)
        if not tmploc:
            tmploc = dest_repo.pathobj / '.git' / 'tmp' / 'datalad-copy'
            tmploc.mkdir(exist_ok=True, parents=True)
            # put in cache for later clean/lookup
            dest_repo_rec['tmp'] = tmploc

        tmploc = tmploc / dest_key
        _replace_file(finfo['objloc'], tmploc, str(tmploc), follow_symlinks=False)

        dest_repo._run_annex_command(
            'reinject',
            annex_options=[str(tmploc), str_dest],
        )

    # are there any URLs defined? Get them by special remote
    # query by key to hopefully avoid additional file system interaction
    whereis = src_repo.whereis(finfo['key'], key=True, output='full')
    urls_by_sr = {
        k: v['urls']
        for k, v in whereis.items()
        if v.get('urls', None)
    }

    if urls_by_sr:
        # some URLs are on record in the for this file
        # obtain information on special remotes
        src_srinfo = src_repo_rec.get('srinfo', None)
        if src_srinfo is None:
            src_srinfo = _extract_special_remote_info(src_repo)
            # put in cache
            src_repo_rec['srinfo'] = src_srinfo
        # TODO generalize to more than one unique dest_repo
        dest_srinfo = _extract_special_remote_info(dest_repo)

        for src_rid, urls in urls_by_sr.items():
            if not (src_rid == '00000000-0000-0000-0000-000000000001' or
                    src_srinfo.get(src_rid, {}).get('externaltype', None) == 'datalad'):
                # TODO generalize to any special remote
                lgr.warn(
                    'Ignore URL for presently unsupported special remote'
                )
                continue
            if src_rid != '00000000-0000-0000-0000-000000000001' and \
                    src_srinfo[src_rid] not in dest_srinfo.values():
                # this is a special remote that the destination repo doesnt know
                sri = src_srinfo[src_rid]
                lgr.debug('Init additionally required special remote: %s', sri)
                dest_repo.init_remote(
                    # TODO what about a naming conflict across all dataset sources?
                    sri['name'],
                    ['{}={}'.format(k, v) for k, v in sri.items() if k != 'name'],
                )
                # must update special remote info for later matching
                dest_srinfo = _extract_special_remote_info(dest_repo)
            for url in urls:
                lgr.debug('Register URL for key %s: %s', dest_key, url)
                dest_repo._run_annex_command(
                    'registerurl', annex_options=[dest_key, url])
            dest_rid = src_rid \
                if src_rid == '00000000-0000-0000-0000-000000000001' \
                else [
                    k for k, v in dest_srinfo.items()
                    if v['name'] == src_srinfo[src_rid]['name']
                ].pop()
            lgr.debug('Mark key %s as present for special remote: %s',
                      dest_key, dest_rid)
            dest_repo._run_annex_command(
                'setpresentkey', annex_options=[dest_key, dest_rid, '1'])

    # TODO prevent copying .git or .datalad of from other datasets
    yield dict(
        path=str_src,
        destination=str_dest,
        message=dest,
        status='ok',
    )


def _replace_file(str_src, dest, str_dest, follow_symlinks):
    if op.lexists(str_dest):
        dest.unlink()
    else:
        dest.parent.mkdir(exist_ok=True, parents=True)
    copyfile(str_src, str_dest, follow_symlinks=False)


def _extract_special_remote_info(repo):
    return {
        k:
        {pk: pv for pk, pv in v.items() if pk != 'timestamp'}
        for k, v in repo.get_special_remotes().items()
    }
