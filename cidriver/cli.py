import click

from collections import OrderedDict
from datetime import datetime
from dateutil.parser import parse as date_parse
from dateutil.tz import (tzoffset, tzlocal, tzutc)
import json
import os
import re
import shlex
from six import string_types
import subprocess
import xml.etree.ElementTree as ET
import yaml

try:
    from shlex import quote as shquote
except ImportError:
    from pipes import quote as shquote

class OrderedLoader(yaml.SafeLoader):
    pass
def __yaml_construct_mapping(loader, node):
    loader.flatten_mapping(node)
    return OrderedDict(loader.construct_pairs(node))
OrderedLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, __yaml_construct_mapping)

class DateTime(click.ParamType):
    name = 'date'
    stamp_re = re.compile(r'^@(?P<utcstamp>\d+)(?:\s+(?P<tzdir>[-+])(?P<tzhour>\d{1,2}):?(?P<tzmin>\d{2}))?$')

    def convert(self, value, param, ctx):
        if value is None or isinstance(value, datetime):
            return value

        try:
            stamp = self.stamp_re.match(value)
            if stamp:
                def int_or_none(i):
                    if i is None:
                        return None
                    return int(i)

                tzdir  = (-1 if stamp.group('tzdir') == '-' else 1)
                tzhour = int_or_none(stamp.group('tzhour'))
                tzmin  = int_or_none(stamp.group('tzmin' ))

                if tzhour is not None:
                    tz = tzoffset(None, tzdir * (tzhour * 3600 + tzmin * 60))
                else:
                    tz = tzutc()
                return datetime.fromtimestamp(int(stamp.group('utcstamp')), tz)

            dt = date_parse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tzlocal())
            return dt
        except ValueError as e:
            self.fail('Could not parse datetime string "{value}": {e}'.format(value=value, e=' '.join(e.args)), param, ctx)

def get_toolchain_image_information(dependency_manifest):
    tree = ET.parse(dependency_manifest)

    def refers_to_toolchain(dependency):
        confAttribute = dependency.get("conf")
        if confAttribute and "toolchain" in confAttribute:
            return True

        for child in dependency:
            if child.tag == "conf":
                mappedAttribute = child.get("mapped")
                if mappedAttribute == "toolchain":
                    return True
        return False

    toolchain_dep, = (
        dep.attrib for dep in tree.getroot().find("dependencies") if refers_to_toolchain(dep))

    return toolchain_dep

@click.group(context_settings=dict(help_option_names=('-h', '--help')))
@click.option('--config', type=click.Path(exists=True, readable=True, resolve_path=True), required=True)
@click.option('--workspace', type=click.Path(exists=True, file_okay=False, dir_okay=True))
@click.option('--dependency-manifest', type=click.File('r'))
@click.pass_context
def cli(ctx, config, workspace, dependency_manifest):
    if ctx.obj is None:
        ctx.obj = {}

    config_dir = os.path.dirname(config)
    def image_from_ivy_manifest(loader, node):
        props = loader.construct_mapping(node) if node.value else {}

        # Fallback to 'dependency_manifest.xml' file in same directory as config
        manifest = (dependency_manifest if dependency_manifest
                else (os.path.join(workspace or config_dir, 'dependency_manifest.xml')))
        image = get_toolchain_image_information(manifest)

        # Override dependency manifest with info from config
        image.update(props)

        # Construct a full, pullable, image path
        image['image'] = os.path.join(*filter(None, (image.get('repository'), image.get('path'), image['name'])))

        return '{image}:{rev}'.format(**image)
    OrderedLoader.add_constructor('!image-from-ivy-manifest', image_from_ivy_manifest)

    with open(config, 'r') as f:
        cfg = yaml.load(f, OrderedLoader)

    volume_vars = {
            'WORKSPACE': workspace or '/tmp/jenkins/' + str(os.getpid()),
        }
    try:
        volume_vars['CT_DEVENV_HOME'] = os.environ['CT_DEVENV_HOME']
    except KeyError:
        pass
    ctx.obj['volume-vars'] = volume_vars
    volumes = []
    for volume in cfg.setdefault('volumes', ['${WORKSPACE}:/code:rw']):
        if isinstance(volume, string_types):
            volume = volume.split(':')
            source = volume.pop(0)
            try:
                target = volume.pop(0)
            except IndexError:
                target = source
            if target.startswith('~/'):
                target = '/home/sandbox' + target[1:]
            try:
                read_only = {'rw': False, 'ro': True}[volume.pop(0)]
            except IndexError:
                read_only = None
            volume = {
                    'source': source,
                    'target': target,
                }
            if read_only is not None:
                volume['read-only'] = read_only
        if 'source' in volume:
            source = os.path.expanduser(volume['source'])

            # Expand variables from our "virtual" environment
            var_re = re.compile(r'\$(?:(\w+)|\{([^}]+)\})')
            last_idx = 0
            new_source = source[:last_idx]
            for var in var_re.finditer(source):
                name = var.group(1) or var.group(2)
                value = volume_vars[name]
                new_source = new_source + source[last_idx:var.start()] + value
                last_idx = var.end()

            new_source = new_source + source[last_idx:]
            # Make relative paths relative to the configuration directory.
            # Absolute paths will be absolute
            source = os.path.join(config_dir, new_source)
            volume['source'] = source
        volumes.append(volume)
    cfg['volumes'] = volumes
    ctx.obj['cfg'] = cfg

@cli.command('checkout-source-tree')
@click.option('--target-remote'     , metavar='<url>')
@click.option('--target-ref'        , metavar='<ref>')
def checkout_source_tree(target_remote, target_ref):
    pass

@cli.command('prepare-source-tree')
# git
@click.option('--target-remote'     , metavar='<url>', help='<target> remote in which to merge <source>')
@click.option('--target-ref'        , metavar='<ref>', help='ref of <target> remote in which to merge <source>')
@click.option('--source-remote'     , metavar='<url>', help='<source> remote to merge into <target>')
@click.option('--source-ref'        , metavar='<ref>', help='ref of <source> remote to merge into <target>')
@click.option('--pull-request'      , metavar='<identifier>'           , help='Identifier of pull-request to use in merge commit message')
@click.option('--pull-request-title', metavar='<title>'                , help='''Pull request title to incorporate in merge commit's subject line''')
@click.option('--author-name'       , metavar='<name>'                 , help='''Name of pull-request's author''')
@click.option('--author-email'      , metavar='<email>'                , help='''E-mail address of pull-request's author''')
@click.option('--author-date'       , metavar='<date>', type=DateTime(), help='''Time of last update to the pull-request''')
# misc
@click.option('--bump-api'          , type=click.Choice(('major', 'minor', 'patch')))
def prepare_source_tree(target_remote, target_ref, source_remote, source_ref, pull_request, pull_request_title, author_name, author_email, author_date, bump_api):
    pass

@cli.command()
@click.pass_context
def phases(ctx):
    cfg = ctx.obj['cfg']
    for phase in cfg['phases']:
        click.echo(phase)

@cli.command()
@click.option('--phase'             , metavar='<phase>'  , help='''Build phase to show variants for''')
@click.pass_context
def variants(ctx, phase):
    variants = []
    cfg = ctx.obj['cfg']
    for phasename, curphase in cfg['phases'].items():
        if phase is not None and phasename != phase:
            continue
        for variant in curphase:
            # Only add when not a duplicate, but preserve order from config file
            if variant not in variants:
                variants.append(variant)
    for variant in variants:
        click.echo(variant)

@cli.command()
@click.option('--phase'             , metavar='<phase>'  , required=True, help='''Build phase''')
@click.option('--variant'           , metavar='<variant>', required=True, help='''Configuration variant''')
@click.pass_context
def getinfo(ctx, phase, variant):
    variants = []
    cfg = ctx.obj['cfg']
    info = {}
    for var in cfg['phases'][phase][variant]:
        if isinstance(var, string_types):
            continue
        var = var.copy()
        for key, val in var.items():
            # TODO: handle recursion over non-string values here

            # Expand variables from our "virtual" environment
            var_re = re.compile(r'\$(?:(\w+)|\{([^}]+)\})')
            last_idx = 0
            new_val = val[:last_idx]
            for var in var_re.finditer(val):
                name = var.group(1) or var.group(2)
                value = ctx.obj['volume-vars'][name]
                new_val = new_val + val[last_idx:var.start()] + value
                last_idx = var.end()

            new_val = new_val + val[last_idx:]
            info[key] = new_val
    click.echo(json.dumps(info))

@cli.command()
@click.option('--ref'               , metavar='<ref>'    , help='''Commit-ish that's checked out and to be built''')
@click.option('--phase'             , metavar='<phase>'  , help='''Build phase to execute''')
@click.option('--variant'           , metavar='<variant>', help='''Configuration variant to build''')
@click.pass_context
def build(ctx, ref, phase, variant):
    cfg = ctx.obj['cfg']
    for phasename, curphase in cfg['phases'].items():
        if phase is not None and phasename != phase:
            continue
        for curvariant, cmds in curphase.items():
            if variant is not None and curvariant != variant:
                continue
            for cmd in cmds:
                if not isinstance(cmd, string_types):
                    try:
                        desc = cmd['description']
                    except (KeyError, TypeError):
                        pass
                    else:
                        click.echo('Performing: ' + click.style(desc, fg='cyan'))
                    try:
                        cmd = cmd['sh']
                    except (KeyError, TypeError):
                        continue

                cmd = shlex.split(cmd)
                # Handle execution inside docker
                if 'image' in cfg:
                    image = cfg['image']
                    if not isinstance(image, string_types):
                        try:
                            image = image[curvariant]
                        except KeyError:
                            image = image['default']
                    uid, gid = os.getuid(), os.getgid()
                    docker_run = ['docker', 'run',
                            '--rm',
                            '--net=host',
                            '--tty',
                            '-e', 'HOME=/home/sandbox',
                            '--tmpfs', '/home/sandbox:uid={},gid={}'.format(uid, gid),
                            '-u', '{}:{}'.format(uid, gid),
                            '-v', '/etc/passwd:/etc/passwd:ro',
                            '-v', '/etc/group:/etc/group:ro',
                            '-w', '/code',
                            '-v', '{WORKSPACE}:/code:rw'.format(**ctx.obj['volume-vars'])
                        ]
                    for volume in cfg['volumes']:
                        param = '{source}:{target}'.format(**volume)
                        try:
                            param = param + ':' + ('ro' if volume['read-only'] else 'rw')
                        except KeyError:
                            pass
                        docker_run += ['-v', param]
                    docker_run.append(image)
                    cmd = docker_run + cmd
                click.echo('Executing: ' + click.style(' '.join(shquote(word) for word in cmd), fg='yellow'))
                subprocess.check_call(cmd)

@cli.command()
@click.option('--target-remote'     , metavar='<url>')
@click.option('--target-ref'        , metavar='<ref>')
@click.option('--ref'               , metavar='<ref>', help='''Commit-ish that has been verified and is to be submitted''')
def submit(target_remote, target_ref, ref):
    pass
