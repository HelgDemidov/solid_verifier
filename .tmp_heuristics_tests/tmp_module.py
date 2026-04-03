
class Router:
    def route(self, req):
        if req.method == "GET":
            pass
        elif req.method == "POST":
            pass
        elif req.method == "PUT":
            pass
        elif isinstance(req, SpecialReq):
            pass
        def _extra(x):
            if x == 1: pass
            elif x == 2: pass
            elif x == 3: pass
            elif x == 4: pass
