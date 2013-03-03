import os
import pygit2
from flask import current_app
from memoize import memoize
import pwd
import pygments.lexers
import stat
from whelk import shell
from goblet.encoding import decode
from collections import defaultdict

class Repository(pygit2.Repository):
    def __init__(self, path):
        if os.path.exists(path):
            super(Repository, self).__init__(path)
        else:
            super(Repository, self).__init__(path + '.git')
        self.gpath = os.path.join(self.path, 'goblet')
        self.cpath = os.path.join(self.gpath, 'cache')
        if not os.path.exists(self.gpath):
            os.mkdir(self.gpath)
        if not os.path.exists(self.cpath):
            os.mkdir(self.cpath)

    @memoize
    def get_description(self):
        desc = os.path.join(self.path, 'description')
        if not os.path.exists(desc):
            return ""
        with open(desc) as fd:
            return decode(fd.read())
    description = property(get_description)

    @memoize
    def get_name(self):
        name = self.path.replace(current_app.config['REPO_ROOT'], '')
        if name.startswith('/'):
            name = name[1:]
        if name.endswith('/.git/'):
            name = name[:-6]
        else:
            name = name[:-5]
        return name
    name = property(get_name)

    @memoize
    def get_clone_urls(self):
        clone_base = current_app.config.get('CLONE_URLS_BASE', {})
        ret = {}
        for proto in ('git', 'ssh', 'http'):
            try:
                ret[proto] = self.config['goblet.cloneurl%s' % proto]
                continue
            except KeyError:
                pass
            if proto not in clone_base:
                continue
            if self.config['core.bare']:
                ret[proto] = clone_base[proto] + os.path.basename(self.path)
            else:
                ret[proto] = clone_base[proto] + os.path.basename(os.path.dirname(os.path.dirname(self.path)))
        return ret
    clone_urls = property(get_clone_urls)

    @memoize
    def get_owner(self):
        try:
            return self.config['goblet.owner']
        except KeyError:
            uid = os.stat(self.path).st_uid
            pwn = pwd.getpwuid(uid)
            if pwn.pw_gecos:
                if ',' in pwn.pw_gecos:
                    return pwn.pw_gecos[:pwn.pw_gecos.find(',')]
                return pwn.pw_gecos
            return pwn.pw_name
    owner = property(get_owner)

    def branches(self):
        return sorted([x[11:] for x in self.listall_references() if x.startswith('refs/heads/')])

    def tags(self):
        return sorted([x[10:] for x in self.listall_references() if x.startswith('refs/tags/')])

    def commit_to_ref_hash(self):
        ret = defaultdict(list)
        for ref in self.listall_references():
            if ref.startswith('refs/remotes/'):
                continue
            if ref.startswith('refs/tags/'):
                obj = self[self.lookup_reference(ref).hex]
                if obj.type == pygit2.GIT_OBJ_COMMIT:
                    ret[obj.hex].append(('tag', ref[10:]))
                else:
                    ret[self[self[obj.target].hex].hex].append(('tag', ref[10:]))
            else:
                ret[self.lookup_reference(ref).hex].append(('head', ref[11:]))
        return ret

    def symref(self, hex):
        if hasattr(hex, 'hex'):
            hex = hex.hex
        for ref in self.listall_references():
            if not ref.startswith('refs/heads/'):
                continue
            if self.lookup_reference(ref).hex == hex:
                return ref[11:]
        return hex

    @property
    def head(self):
        try:
            return super(Repository, self).head
        except pygit2.GitError:
            return None

    def get_commits(self, ref, skip, count, search=None):
        num = 0
        for commit in self.walk(ref.hex, pygit2.GIT_SORT_TIME):
            if search and search not in commit.message:
                continue
            num += 1
            if num < skip:
                continue
            if num >= skip + count:
                break
            yield commit

    def describe(self, commit):
        tags = [self.lookup_reference(x) for x in self.listall_references() if x.startswith('refs/tags')]
        if not tags:
            return 'g' + commit[:7]
        tags = [(tag.name[10:], self[tag.hex]) for tag in tags]
        tags = dict([(hasattr(obj, 'target') and self[obj.target].hex or obj.hex, name) for name, obj in tags])
        count = 0
        for parent in self.walk(commit, pygit2.GIT_SORT_TIME):
            if parent.hex in tags:
                if count == 0:
                    return tags[parent.hex]
                return '%s-%d-g%s' % (tags[parent.hex], count, commit[:7])
            count += 1
        return 'g' + commit[:7]

    def ls_tree(self, tree, path=''):
        ret = []
        for entry in tree:
            if stat.S_ISDIR(entry.filemode):
                ret += self.ls_tree(entry.to_object(), os.path.join(path, entry.name))
            else:
                ret.append(os.path.join(path, entry.name))
        return ret

    def tree_lastchanged(self, commit, path):
        """Get a dict of {name: hex} for commits that last changed files in a directory"""
        data = self.git('blame-tree', '--max-depth=1', commit.hex, '--', os.path.join('.', path)).stdout
        data = data.decode('utf-8').splitlines()
        data = [x.split() for x in data]
        if path:
            data = [(p[p.rfind('/')+1:], m) for (m,p) in data]
        else:
            data = [(p, m) for (m,p) in data]
        return dict(data)

    def blame(self, commit, path):
        if hasattr(commit, 'hex'):
            commit = commit.hex
        contents = self.git('blame', '-p', commit, '--', path).stdout.splitlines()
        commits = {}
        last_commit = None
        lines = []
        orig_line = line_now = 0
        for line in contents:
            if not last_commit:
                last_commit, orig_line, line_now = line.split()[:3]
                if last_commit not in commits:
                    commits[last_commit] = {'hex': last_commit}
            elif line.startswith('\t'):
                lines.append((line[1:], orig_line, line_now, commits[last_commit]))
                last_commit = None
            elif line == 'boundary':
                commits[last_commit]['previous'] = None
            else:
                key, val = line.split(None, 1)
                commits[last_commit][key] = val
        return lines

    def grep(self, commit, path, query):
        if hasattr(commit, 'hex'):
            commit = commit.hex
        results = self.git('grep', '-n', '--full-name', '-z', '-I', '-C1', '--heading', '--break', query, commit, '--', path).stdout.strip()
        files = results.split('\n\n')
        for file in files:
            chunks = []
            for chunk in [x.split('\n') for x in file.split('\n--\n')]:
                chunks.append([line.split('\0') for line in chunk])
            filename = chunks[0].pop(0)[0].split(':', 1)
            yield filename, chunks

    def git(self, *args):
        return shell.git('--git-dir', self.path, *args)

def get_tree(tree, path):
    for dir in path:
        if dir not in tree:
            return None
        tree = tree[dir].to_object()
    return tree

pygit2.Repository = Repository

def S_ISGITLNK(mode):
    return (mode & 0160000) == 0160000
stat.S_ISGITLNK = S_ISGITLNK

# Let's detect .pl as perl instead of prolog
pygments.lexers.LEXERS['PrologLexer'] = ('pygments.lexers.compiled', 'Prolog', ('prolog',), ('*.prolog', '*.pro'), ('text/x-prolog',))
