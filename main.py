from django.utils import simplejson as json

from google.appengine.api import memcache
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp import util

import colorsys
import string
import functools, sha
from datetime import datetime, timedelta

class EnumProperty(db.Property):
    """
    Maps a list of strings to be saved as int. The property is set or get using
    the string value, but it is stored using its index in the 'choices' list.
    """
    data_type = int

    def __init__(self, choices=None, **kwargs):
        if not isinstance(choices, list):
            raise TypeError('Choices must be a list.')
        super(EnumProperty, self).__init__(choices=choices, **kwargs)

    def get_value_for_datastore(self, model_instance):
        value = self.__get__(model_instance, model_instance.__class__)
        if value is not None:
            return int(self.choices.index(value))

    def make_value_from_datastore(self, value):
        if value is not None:
            return self.choices[int(value)]

    def empty(self, value):
        return value is None

class ColorModel(db.Model):

    color_lists = ['default', 'resene', 'html4', 'css3']

    n = db.StringProperty(required=True)
    t = EnumProperty(choices=color_lists,required=True)
    r = db.FloatProperty(required=True)
    g = db.FloatProperty(required=True)
    b = db.FloatProperty(required=True)
    h = db.FloatProperty(required=True)
    s = db.FloatProperty(required=True)
    l = db.FloatProperty(required=True)

    @classmethod
    def get_or_insert2(cls, key_name, **kwds):
        def txn():
            entity = cls.get_by_key_name(key_name, parent=kwds.get
    ('parent'))
            if entity is None:
                entity = cls(key_name=key_name, **kwds)
                entity.put()
                return (True,entity)
            return (False,entity)
        return db.run_in_transaction(txn)

# request handler
class ReqHandler(webapp.RequestHandler):
    def get_header(self, header_string):
        return str(self.request.headers[header_string])

    def raise_error(self, code, msg):
        self.error(code)
        self.response.out.write(msg)

# ratelimit decorator
class ratelimit(object):
    minutes = 1 # The time period
    requests = 20 # Number of allowed requests in that time period

    prefix = 'rl_' # Prefix for memcache key

    def __init__(self, **options):
        for key, value in options.items():
            setattr(self, key, value)

    def __call__(self, fn):
        def wrapper(reqhandler, *args, **kwargs):
            return self.view_wrapper(reqhandler, fn, *args, **kwargs)
        functools.update_wrapper(wrapper, fn)
        return wrapper

    def view_wrapper(self, reqhandler, fn, *args, **kwargs):
        if not self.should_ratelimit(reqhandler):
            return fn(reqhandler, *args, **kwargs)

        counts = self.get_counters(reqhandler).values()

        # Increment rate limiting counter
        self.cache_incr(self.current_key(reqhandler))

        # Have they failed?
        if sum(counts) >= self.requests:
            return self.disallowed(reqhandler)

        return fn(reqhandler, *args, **kwargs)

    def cache_get_many(self, keys):
        return memcache.get_multi(keys)

    def cache_incr(self, key):
        # add key fails if the key exists
        memcache.add(key, 0, time=self.expire_after())
        memcache.incr(key)

    def should_ratelimit(self, reqhandler):
        return True

    def get_counters(self, reqhandler):
        return self.cache_get_many(self.keys_to_check(reqhandler))

    def keys_to_check(self, reqhandler):
        extra = self.key_extra(reqhandler)
        now = datetime.now()
        return [
            '%s%s-%s' % (
                self.prefix,
                extra,
                (now - timedelta(minutes = minute)).strftime('%Y%m%d%H%M')
            ) for minute in range(self.minutes + 1)
        ]

    def current_key(self, reqhandler):
        return '%s%s-%s' % (
            self.prefix,
            self.key_extra(reqhandler),
            datetime.now().strftime('%Y%m%d%H%M')
        )

    def key_extra(self, reqhandler):
            return reqhandler.request.remote_addr

    def disallowed(self, reqhandler):
        reqhandler.response.clear()
        reqhandler.response.set_status(403)
        reqhandler.response.out.write("too many requests")

    def expire_after(self):
        "Used for setting the memcached cache expiry"
        return (self.minutes + 1) * 60

class LookupHandler(ReqHandler):

    def hex_to_rgb(req,value):
        value = value.lstrip('#')
        lv = len(value)
        return tuple(int(value[i:i+lv/3], 16) for i in range(0, lv, lv/3))

    def rgb_to_hex(req,rgb):
        return '#%02x%02x%02x' % rgb

    def hex_to_rgbfloat(req,value):
        return map(lambda x: float(x)/255, req.hex_to_rgb(value))

    def rgbfloat_to_hex(req,rgbfl):
        return req.rgb_to_hex(tuple(map(lambda x: int(x*255), rgbfl)))

    def standarize_hex(req,hexnumber):
        hexnumber = str(hexnumber).strip().lstrip('#')
        if len(hexnumber) > 6:
            hexnumber = hexnumber[:6]
            for c in hexnumber:
                if c not in string.hexdigits:
                    hexnumber = '000000'
                    break
        elif len(hexnumber) <= 6:
            for c in hexnumber:
                if c not in string.hexdigits:
                    hexnumber = '000000'
                    break
            hexnumber = hexnumber.zfill(6)
        return hexnumber.lower()

    def find_match(req,hexnumber,list):
        r,g,b = req.hex_to_rgbfloat(hexnumber)
        h,l,s = colorsys.rgb_to_hls(r,g,b)

        ndf = 0
        color = None
        df = -1

        q = ColorModel.all().filter('t =', ColorModel.color_lists.index(list))

        results = q.fetch(limit=10000)

        for c in results:
            ndf = ((r - c.r) ** 2) + ((g - c.g) ** 2) + ((b - c.b) ** 2) + (((h - c.h) ** 2) + ((s - c.s) ** 2) + ((l - c.l) ** 2)) * 2
            if not 0 < df < ndf:
                df = ndf
                color = c

        if not color:
            return ColorModel.get_by_key_name('0000000')
        else:
            return color

    def handle_req(req):
        hexnumber = req.standarize_hex(req.request.get('hex', default_value='000000'))
        list = req.request.get('list', default_value='default').lower()
        color = ColorModel.get_by_key_name(str(ColorModel.color_lists.index(list))+hexnumber)
        json_format = req.request.get('format_json', default_value='0') == '1'
        if color is not None:
            req.format_req(color,True, json_format)
        else:
            color = req.find_match(hexnumber,list)
            req.format_req(color,False, json_format)

    def format_req(req,color,match,in_json=False):
        if in_json:
            req.response.headers['Content-Type'] = "application/json; charset=utf-8"
            req.response.out.write(json.dumps({
                "name": color.n,
                "color": req.rgbfloat_to_hex((color.r, color.g, color.b)),
                "list:": color.t,
                "match": match
            }))
        else:
            req.response.headers['Content-Type'] = "text/plain; charset=utf-8"
            req.response.out.write(color.n + "\n" + req.rgbfloat_to_hex((color.r, color.g, color.b)) + "\n" + color.t + "\n" + str(match))

    @ratelimit()
    def get(self):
        self.handle_req()

    @ratelimit()
    def post(self):
        self.handle_req()

class MainHandler(ReqHandler):
    def get(self):
        self.response.out.write('Hello world!')

def main():
    application = webapp.WSGIApplication([
                                            ('/', MainHandler),
                                            ('/match', LookupHandler)
                                            ],
                                         debug=False)
    util.run_wsgi_app(application)


if __name__ == '__main__':
    main()
