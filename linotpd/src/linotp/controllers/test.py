# For testing.

from flask import request

from . import BaseController, methods


class TestController(BaseController):
    @methods(['GET', 'POST'])
    def testmethod(self):
        return 'method:' + request.method

    @methods(['GET'])
    def testmethod2(self):
        return 'method:' + request.method

    def testmethod3(self):
        return 'method:' + request.method

    def testmethod_args(self, s, t):
        return 'method:' + request.method + ',' + ','.join([s, t])
