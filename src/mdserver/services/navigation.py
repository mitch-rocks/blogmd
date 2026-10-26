"""Builds the sidebar tree from post paths."""
from __future__ import annotations

from dataclasses import dataclass, field

from mdserver.domain import Post, PostStatus
from mdserver.storage import Database


@dataclass
class NavNode:
    name: str                     # last path segment
    path: str                     # full path below /blog/
    title: str
    has_page: bool = False        # a post exists at exactly this path
    order: int = 0
    children: list['NavNode'] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f'/blog/{self.path}/' if self.path else '/blog/'

    @property
    def is_folder(self) -> bool:
        return bool(self.children)

    @property
    def label(self) -> str:
        """Sidebar text: folders use their segment name, pages their title."""
        return _pretty(self.name) if (self.is_folder and self.path) else self.title

    def contains(self, path: str) -> bool:
        return path == self.path or (self.path == '' or path.startswith(self.path + '/'))


def _pretty(segment: str) -> str:
    return segment.replace('-', ' ').replace('_', ' ').strip().title() or 'Home'


def build_tree(posts: list[Post]) -> NavNode:
    root = NavNode(name='', path='', title='Home')
    index: dict[str, NavNode] = {'': root}

    for post in sorted(posts, key=lambda p: p.path):
        if post.path.startswith('_'):
            continue
        parts = post.path.split('/') if post.path else []
        node = root
        for i, seg in enumerate(parts):
            sub = '/'.join(parts[: i + 1])
            if sub not in index:
                child = NavNode(name=seg, path=sub, title=_pretty(seg))
                node.children.append(child)
                index[sub] = child
            node = index[sub]
        node.has_page = True
        node.title = post.title
        node.order = post.nav_order

    def sort(n: NavNode):
        n.children.sort(key=lambda c: (c.order, not c.is_folder, c.path))
        for c in n.children:
            sort(c)

    sort(root)
    return root


async def navigation(db: Database) -> NavNode:
    return build_tree(await db.posts.list(statuses=[PostStatus.PUBLISHED]))


def breadcrumbs(tree: NavNode, path: str) -> list[NavNode]:
    crumbs, node = [tree], tree
    parts = path.split('/') if path else []
    for i in range(len(parts)):
        sub = '/'.join(parts[: i + 1])
        nxt = next((c for c in node.children if c.path == sub), None)
        if nxt is None:
            crumbs.append(NavNode(name=parts[i], path=sub, title=_pretty(parts[i])))
            break
        crumbs.append(nxt)
        node = nxt
    return crumbs


def find(tree: NavNode, path: str) -> NavNode | None:
    if tree.path == path:
        return tree
    for c in tree.children:
        if c.contains(path):
            hit = find(c, path)
            if hit:
                return hit
    return None
