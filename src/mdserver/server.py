import os
import flask
import uvicorn
from asgiref.wsgi import WsgiToAsgi
import pathlib
from .util import make_default_dirs, send_file_raw, send_blog_page
from mdserver.var import DEBUG, ROOT_FOLDER, STATIC_FOLDER

from mdserver.models.page import BlogPage



def get_file(filename):
    # return 404 if file doesn't exist
    if f'{filename}' not in os.listdir(STATIC_FOLDER) : return send_blog_page('error/404.md'), 404
    return flask.send_file(STATIC_FOLDER / filename)


def start(host:str='0.0.0.0', port:int=8081) -> flask.Flask:    
    app = flask.Flask('markdown-server', static_folder=STATIC_FOLDER, template_folder=ROOT_FOLDER)
    app.url_map.strict_slashes = False

    # /favicon.ico
    @app.route('/favicon.ico', methods=['GET'])
    def favicon() : return BlogPage('error/404').render(), 404

    # /
    @app.route('/', methods=['GET'])
    def root() : return flask.redirect('/blog/', code=308)

    # # /blog/
    # @app.route('/blog/', methods=['GET'])
    # def blog_root() : return '/blog/example-md', 304

    @app.route('/<path:branch>/', methods=['GET'])
    def get_file(branch):
        #
        # endpoint for general fetching blogs and files
        # via /blog/ and /file/ respectively
        #
        split_branch = list(filter(None, branch.split('/')))
        domain = split_branch[0]
        match domain:
            case 'static':
                path = os.path.join(*split_branch)
                return send_file_raw(path)
            case 'blog':
                try : return BlogPage(path=branch).render()
                except : return BlogPage(path='error/404').render()
            case _:
                return 'error', 404
        


    make_default_dirs()

    print(f'{DEBUG=}')

    if DEBUG:
        app.run(host, port, DEBUG)
    else:
        asgi_app = WsgiToAsgi(app)
        uvicorn.run(asgi_app, host=host, port=port)
