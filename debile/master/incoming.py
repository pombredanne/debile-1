# Copyright (c) 2012-2013 Paul Tagliamonte <paultag@debian.org>
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import os
import fnmatch
from debian import deb822

from firewoes.lib.hash import idify, uniquify
from sqlalchemy.orm.exc import NoResultFound, MultipleResultsFound

from debile.master.reprepro import Repo, RepoSourceAlreadyRegistered
from debile.master.filerepo import FileRepo, FilesAlreadyRegistered
from debile.utils.dud import Dud, DudFileException
from debile.master.utils import session
from debile.master.messaging import emit
from debile.master.orm import (Person, Builder, Suite, Component, Arch, Check,
                               Group, GroupSuite, Source, Maintainer, Binary,
                               Job, JobDependencies, Result,
                               create_source, create_jobs)
from debile.utils.changes import parse_changes_file, ChangesFileException


def process_directory(path):
    with session() as s:
        abspath = os.path.abspath(path)
        for fp in os.listdir(abspath):
            path = os.path.join(abspath, fp)
            for glob, handler in DELEGATE.items():
                if fnmatch.fnmatch(path, glob):
                    handler(s, path)
                    break


def process_changes(session, path):
    changes = parse_changes_file(path)
    try:
        changes.validate()
    except ChangesFileException as e:
        return reject_changes(session, changes, "invalid-upload")

    group = changes.get('X-Lucy-Group', "default")
    try:
        group = session.query(Group).filter_by(name=group).one()
    except MultipleResultsFound:
        return reject_changes(session, changes, "internal-error")
    except NoResultFound:
        return reject_changes(session, changes, "invalid-group")

    if not group.debile_should_process_changes:
        # This file isn't for us, just leave it.
        return

    try:
        key = changes.validate_signature()
    except ChangesFileException:
        return reject_changes(session, changes, "invalid-signature")

    #### Sourceful Uploads
    if changes.is_source_only_upload():
        try:
            user = session.query(Person).filter_by(key=key).one()
        except NoResultFound:
            return reject_changes(session, changes, "invalid-user")
        return accept_source_changes(session, changes, user)

    #### Binary Uploads
    if changes.is_binary_only_upload():
        try:
            builder = session.query(Builder).filter_by(key=key).one()
        except NoResultFound:
            return reject_changes(session, changes, "invalid-builder")
        return accept_binary_changes(session, changes, builder)

    return reject_changes(session, changes, "mixed-upload")


def reject_changes(session, changes, tag):

    print "REJECT: {source} because {tag}".format(
        tag=tag, source=changes.get_package_name())

    emit('reject', 'source', {
        "tag": tag,
        "source": changes.get_package_name(),
    })

    for fp in [changes.get_filename()] + changes.get_files():
        os.unlink(fp)
    # Note this in the log.


def accept_source_changes(session, changes, user):
    group = changes.get('X-Lucy-Group', "default")
    suite = changes['Distribution']

    try:
        group_suite = session.query(GroupSuite).filter(
            Group.name==group,
            Suite.name==suite,
        ).one()
    except MultipleResultsFound:
        return reject_changes(session, changes, "internal-error")
    except NoResultFound:
        return reject_changes(session, changes, "invalid-suite-for-group")

    dsc = changes.get_dsc_obj()
    if dsc['Source'] != changes['Source']:
        return reject_changes(session, changes, "dsc-does-not-march-changes")
    if dsc['Version'] != changes['Version']:
        return reject_changes(session, changes, "dsc-does-not-march-changes")

    try:
        source = session.query(Source).filter(
            Source.name==dsc['Source'],
            Source.version==dsc['Version'],
            GroupSuite.group==group_suite.group,
        ).one()
        return reject_changes(session, changes, "source-already-in-group")
    except MultipleResultsFound:
        return reject_changes(session, changes, "internal-error")
    except NoResultFound:
        pass

    component = session.query(Component).filter_by(name="main").one()
    arches = dsc['Architecture'].split()

    source = create_source(dsc, group_suite, component, user)
    create_jobs(source)

    session.add(source)  # OK. Populated entry. Let's insert.
    session.commit()  # Neato.

    # OK. We have a changes in order. Let's roll.
    repo = Repo(group_suite.group.repo_path)
    repo.add_changes(changes)

    emit('accept', 'source', source.debilize())

    # OK. It's safely in the database and repo. Let's cleanup.
    for fp in [changes.get_filename()] + changes.get_files():
        os.unlink(fp)


def accept_binary_changes(session, changes, builder):
    # OK. We'll relate this back to a build job.
    job = changes.get('X-Debile-Job', None)
    if job is None:
        return reject_changes(session, changes, "no-job")
    job = session.query(Job).get(job)
    source = job.source

    if changes.get('Source') != source.name:
        return reject_changes(session, changes, "binary-source-name-mismatch")

    if changes.get("Version") != source.version:
        return reject_changes(session, changes, "binary-source-version-mismatch")

    if changes.get('X-Lucy-Group', "default") != source.group_suite.group.name:
        return reject_changes(session, changes, "binary-source-group-mismatch")

    if changes.get('Distribution') != source.group_suite.suite.name:
        return reject_changes(session, changes, "binary-source-suite-mismatch")

    if changes.get("Architecture") != job.arch.name:
        return reject_changes(session, changes, "wrong-architecture")

    if builder != job.builder:
        return reject_changes(session, changes, "wrong-builder")

    binary = Binary.from_job(job)

    ## OK. Let's make sure we can add this.
    try:
        repo = Repo(job.source.group_suite.group.repo_path)
        repo.add_changes(changes)
    except RepoSourceAlreadyRegistered:
        return reject_changes(session, changes, 'stupid-source-thing')

    job.changes_uploaded(session, binary)
    session.add(binary)
    session.commit()

    emit('accept', 'binary', binary.debilize())

    # OK. It's safely in the database and repo. Let's cleanup.
    for fp in [changes.get_filename()] + changes.get_files():
        os.unlink(fp)


def process_dud(session, path):
    dud = Dud(filename=path)
    jid = dud.get("X-Debile-Job", None)
    if jid is None:
        return reject_dud(session, dud, "missing-dud-job")

    try:
        dud.validate()
    except DudFileException as e:
        return reject_dud(session, dud, "invalid-dud-upload")

    key = dud.validate_signature()

    try:
        builder = session.query(Builder).filter_by(key=key).one()
    except NoResultFound:
        return reject_dud(session, dud, "invalid-dud-builder")

    try:
        job = session.query(Job).get(jid)
    except NoResultFound:
        return reject_dud(session, dud, "invalid-dud-job")

    if dud.get("X-Debile-Failed", None) is None:
        return reject_dud(session, dud, "no-failure-notice")

    if job.builder != builder:
        return reject_dud(session, dud, "invalid-dud-uploader")

    accept_dud(session, dud, builder)


def reject_dud(session, dud, tag):
    print "REJECT: {source} because {tag}".format(
        tag=tag, source=dud['Source'])

    e = None
    try:
        dud.validate()
    except DudFileException as e:
        print e

    emit('reject', 'result', {
        "tag": tag,
        "source": dud['Source'],
    })

    for fp in [dud.get_filename()] + dud.get_files():
        os.unlink(fp)
    # Note this in the log.


def accept_dud(session, dud, builder):
    fire = dud.get_firehose()
    failed = True if dud.get('X-Debile-Failed', None) == "Yes" else False

    job = session.query(Job).get(dud['X-Debile-Job'])

    fire, _ = idify(fire)
    fire = uniquify(session, fire)

    result = Result.from_job(job)
    result.failed = failed
    result.firehose = fire
    session.merge(result)  # Needed because a *lot* of the Firehose is 
    # going to need unique ${WORLD}.

    job.dud_uploaded(session, result)
    session.commit()  # Neato.

    try:
        repo = FileRepo(job.source.group_suite.group.files_path)
        repo.add_dud(dud)
    except FilesAlreadyRegistered:
        return reject_dud(session, dud, "dud-files-already-registered")

    emit('receive', 'result', result.debilize())
    #  repo.add_dud removes the files


DELEGATE = {
    "*.changes": process_changes,
    "*.dud": process_dud,
}
