import os
import time
import ctypes
import string

class FuncPtrWrapper():

    def __init__(self, lib, name):
        self.lib = lib
        self.name = name
    
    def __call__(self, *args):
        raise ValueError("Function not declared")

    def decl(self, rt, ats):
        getattr(self.lib.lib, self.name).restype = rt
        getattr(self.lib.lib, self.name).argtypes = tuple(ats)
        self.lib.decld_fptrs.add(self.name)

class Lib():
    
    def __init__(self, path):
        if os.name == "nt":
            self.lib = ctypes.windll.LoadLibrary(path)
        else:
            self.lib = ctypes.CDLL(path)
        self.decld_fptrs = set()
    
    def __getattr__(self, name):
        if hasattr(self.lib, name):
            if name in self.decld_fptrs:
                return getattr(self.lib, name)
            return FuncPtrWrapper(self, name)
        raise AttributeError(name)

    def __dir__(self):
        return sorted(self.decld_fptrs)

class LuaRuntimeError(RuntimeError):
    pass

class _LuaReferenceContainer():

    def __init__(self, thread):
        self.thread = thread
        self.ref = lua54.luaL_ref(self.thread.L, lua54.LUA_REGISTRYINDEX)
        lua54.lua_pushnil(self.thread.L)

    def __del__(self):
        lua54.luaL_unref(self.thread.L, lua54.LUA_REGISTRYINDEX, self.ref)

    def _pushrefval(self):
        lua54.lua_rawgeti(self.thread.L, lua54.LUA_REGISTRYINDEX, self.ref)

class Function(_LuaReferenceContainer):

    def __repr__(self):
        self._pushrefval()
        if lua54.lua_type(self.thread.L, -1) != lua54.LUA_TFUNCTION:
            return "<invalid function>"
        s = "function: 0x%x" % (lua54.lua_topointer(self.thread.L, -1))
        lua54.lua_pop(self.thread.L, 1)
        return s

    def __hash__(self):
        self._pushrefval()
        r = lua54.lua_topointer(self.thread.L, -1)
        lua54.lua_pop(self.thread.L, 1)
        return r

    async def __call__(self, *args):
        coro = Coroutine(self.thread.runtime, args)
        self._pushrefval()
        lua54.lua_xmove(self.thread.L, coro.L, 1)
        async for _ in coro:
            pass
        return coro.return_value

class Table(_LuaReferenceContainer):
    
    def __getitem__(self, k):
        try:
            self._pushrefval()
            
            if lua54.lua_type(self.thread.L, -1) != lua54.LUA_TTABLE:
                raise ValueError("Invalid table")
            
            if isinstance(k, str):
                s = k.encode(self.thread.runtime.encoding)
                lua54.lua_pushlstring(self.thread.L, s, len(s))

            elif k is True:
                lua54.lua_pushboolean(self.thread.L, 1)

            elif k is False:
                lua54.lua_pushboolean(self.thread.L, 0)

            elif isinstance(k, _LuaReferenceContainer):
                k._pushrefval()
            
            else:
                k = int(k)
                lua54.lua_pushnumber(self.thread.L, k)
            
            luavalue = lua54.lua_gettable(self.thread.L, -2)
            pyvalue = self.thread._to_python_type(-1)
            lua54.lua_pop(self.thread.L, 2)
            return pyvalue
        except OSError:
            print("OSError caught: stack top is", lua54.lua_gettop(self.thread.L))
            raise

    def lua_rawequal(self, other):
        if not isinstance(other, Table):
            return False
        self._pushrefval()
        other._pushrefval()
        res = lua54.lua_rawequal(self.thread.L, -1, -2) > 0
        lua54.lua_pop(self.thread.L, 2)
        return res

    def __eq__(self, other):
        if not isinstance(other, Table):
            return False
        self._pushrefval()
        other._pushrefval()
        res = lua54.lua_compare(self.thread.L, -1, -2, lua54.LUA_OPEQ) > 0
        lua54.lua_pop(self.thread.L, 2)
        return res

    def _getc(self, i):
        contents = []
        self._pushrefval()
        lua54.lua_pushnil(self.thread.L)
        while lua54.lua_next(self.thread.L, -2) != 0:
            contents.append(self.thread._to_python_type(i))
            lua54.lua_pop(self.thread.L, 1)
        lua54.lua_pop(self.thread.L, 1)
        return contents

    def keys(self):
        return self._getc(-2)

    def values(self):
        return self._getc(-1)

    def items(self):
        return zip(self.keys(), self.values())

    def __iter__(self):
        return iter(self.keys())

    def __contains__(self, key):
        return self[key] is not None

    REPRD_TABLES = None
    def __repr__(self):
        def valid_name(name):
            if not isinstance(name, str):
                return False

            allowed = string.ascii_letters + string.digits + "_"
            for i in name:
                if i not in allowed:
                    return False
            return True

        try:
            clear = False
            if Table.REPRD_TABLES is None:
                Table.REPRD_TABLES = []
                clear = True
            else:
                for other in Table.REPRD_TABLES:
                    if self.lua_rawequal(other):
                        return "..."
            
            Table.REPRD_TABLES.append(self)
            s = "{" + ", ".join("%s=%r" % (k, v) if valid_name(k) else (repr(v) if type(k) is float else "[%r]=%r" % (k, v)) for k, v in sorted(self.items(), key=lambda k: -k if type(k) == int else 1)) + "}"

        finally:
            if clear:
                Table.REPRD_TABLES = None
        
        return s            

class Coroutine():

    def __init__(self, runtime, args, dummy=False):
        self.started = False
        self.ended = dummy
        self.runtime = runtime
        self.args = args
        self.return_value = None
        self._nargs = 0
        self.callback_results = None

        try:
            self.L = lua54.lua_newthread(self.runtime.L)
            self.ref = lua54.luaL_ref(self.runtime.L, lua54.LUA_REGISTRYINDEX)
        except OSError:
            print("OSError caught: stack top is", lua54.lua_gettop(runtime.L))
            raise

    def __del__(self):
        lua54.luaL_unref(self.runtime.L, lua54.LUA_REGISTRYINDEX, self.ref)
    
    def __aiter__(self):
        return self

    async def __anext__(self):        
        if self.ended:
            raise StopAsyncIteration
        
        if not self.started:
            for i in self.args:
                self._push_python_object(i)
            self._nargs = len(self.args)
            self.args = None
            self.started = True

        if self.callback_results is not None:
            if isinstance(self.callback_results, tuple):
                for i in self.callback_results:
                    self._push_python_object(i)
                    self._nargs += 1
            else:
                self._push_python_object(self.callback_results)
                self._nargs += 1
            
            self.callback_results = None
        
        nresults = ctypes.c_int(0)
        ecode = lua54.lua_resume(self.L, None, self._nargs, ctypes.pointer(nresults))
        self._nargs = 0
        if ecode == lua54.LUA_OK:
            self.ended = True
            self.return_value = []
            for _ in range(lua54.lua_gettop(self.L)):
                self.return_value.append(self._to_python_type(-1))
                lua54.lua_pop(self.L, 1)
            self.return_value = tuple(reversed(self.return_value))
            raise StopAsyncIteration

        elif ecode == lua54.LUA_ERRRUN:
            raise LuaRuntimeError(lua54.lua_tolstring(self.L, -1, None).decode(self.runtime.encoding))

        elif ecode == lua54.LUA_ERRMEM:
            raise MemoryError

        elif ecode == lua54.LUA_ERRERR:
            raise ValueError("error handler function failed")
        
        expected_nresults = int(lua54.lua_tonumberx(self.L, -1, None))
        lua54.lua_pop(self.L, 1)
        
        command_name = lua54.lua_tolstring(self.L, -1, None).decode(self.runtime.encoding)
        lua54.lua_pop(self.L, 1)
        
        if expected_nresults != nresults.value - 2:
            raise ValueError("Command %r expected %d arguments, got %d" % (command_name, expected_nresults, nresults.value - 2))

        self.callback_results = await self.runtime.callbacks[command_name](self.runtime, *self._get_args(expected_nresults))
    
    def _get_args(self, n):
        results = []
        for _ in range(n):
            results.append(self._to_python_type(-1))
            lua54.lua_pop(self.L, 1)
        return results[::-1]

    def _push_python_object(self, obj):
        if obj is None:
            lua54.lua_pushnil(self.L)

        elif obj is False:
            lua54.lua_pushboolean(self.L, 0)

        elif obj is True:
            lua54.lua_pushboolean(self.L, 1)

        elif isinstance(obj, (int, float)):
            lua54.lua_pushnumber(self.L, obj)

        elif isinstance(obj, bytes):
            lua54.lua_pushlstring(self.L, obj, len(obj))

        elif isinstance(obj, str):
            obj = obj.encode(self.runtime.encoding)
            lua54.lua_pushlstring(self.L, obj, len(obj))
        
        elif isinstance(obj, Table):
            obj._pushrefval()
        
        else:
            raise ValueError("Cannot convert %r into a Lua type" % obj)

    def _to_python_type(self, item_n):
        item_type = lua54.lua_type(self.L, item_n)
        if item_type == lua54.LUA_TNIL:
            return None
        
        elif item_type == lua54.LUA_TBOOLEAN:
            return lua54.lua_toboolean(self.L, item_n) > 0
        
        elif item_type == lua54.LUA_TNUMBER:
            return lua54.lua_tonumberx(self.L, item_n, None)

        elif item_type == lua54.LUA_TSTRING:
            sz = size_t(0)
            s = lua54.lua_tolstring(self.L, item_n, ctypes.pointer(sz))
            if s is None:
                return ""
            return s[:sz.value].decode(self.runtime.encoding)

        elif item_type == lua54.LUA_TTABLE:
            refs = []
            if item_n > 0:
                item_n = lua54.lua_gettop(self.L) - item_n - 1
            for i in range(-item_n - 1):
                refs.append(lua54.luaL_ref(self.L, lua54.LUA_REGISTRYINDEX))
            
            t = Table(self)
            
            for i in reversed(refs):
                lua54.lua_rawgeti(self.L, lua54.LUA_REGISTRYINDEX, i)
                lua54.luaL_unref(self.L, lua54.LUA_REGISTRYINDEX, i)
            
            return t

        elif item_type == lua54.LUA_TFUNCTION:
            refs = []
            if item_n > 0:
                item_n = lua54.lua_gettop(self.L) - item_n - 1
            for i in range(-item_n - 1):
                refs.append(lua54.luaL_ref(self.L, lua54.LUA_REGISTRYINDEX))
            
            f = Function(self)
            
            for i in reversed(refs):
                lua54.lua_rawgeti(self.L, lua54.LUA_REGISTRYINDEX, i)
                lua54.luaL_unref(self.L, lua54.LUA_REGISTRYINDEX, i)
            
            return f

        else:
            raise ValueError("Cannot convert %s to Python type" % ([
                "nil", "boolean", "lightuserdata", "number", "string", "table", "function", "userdata", "thread"
            ][item_type]))

class Runtime():
    
    def __init__(self, code, encoding="ascii"):
        self._CFUNCTIONS = []
        self.encoding = encoding
        
        self.L = lua54.luaL_newstate()
        lua54.luaL_loadstring(self.L, code.encode(self.encoding))
        if lua54.lua_type(self.L, -1) == lua54.LUA_TSTRING:
            raise ValueError(lua54.lua_tolstring(self.L, -1, None).decode(self.encoding))
        
        lua54.luaopen_base(self.L)
        lua54.lua_pop(self.L, 1)
        
        ecode = lua54.lua_pcallk(self.L, 0, 0, 0, 0, None)
        if ecode == lua54.LUA_YIELD:
            raise AssertionError("lua_pcallk returned LUA_YIELD")

        elif ecode == lua54.LUA_ERRERR:
            raise AssertionError("lua_pcallk returned LUA_ERRERR")

        elif ecode == lua54.LUA_ERRRUN:
            raise LuaRuntimeError(lua54.lua_tolstring(self.L, -1, None).decode(self.encoding))

        elif ecode == lua54.LUA_ERRMEM:
            if self.lua54.lua_gettop(self.L) > 0:
                raise MemoryError(lua54.lua_tolstring(self.L, -1, None).decode(self.encoding))
            raise MemoryError

        self.dummy_coroutine = Coroutine(self, (), dummy=True)

        self.callbacks = {}
    
    def _register(self, name, f):
        cf = lua54.lua_CFunction(f)
        self._CFUNCTIONS.append(cf)
        lua54.lua_pushcclosure(self.L, cf, 0)
        lua54.lua_setglobal(self.L, name)
    
    def __del__(self):
        self.lua54.lua_close(self.L)

    def globals(self):
        try:
            lua54.lua_getglobal(self.dummy_coroutine.L, b"_G")
            t = Table(self.dummy_coroutine)
            lua54.lua_pop(self.dummy_coroutine.L, 1)
            return t
        except OSError:
            print("OSError caught: stack top is", lua54.lua_gettop(self.dummy_coroutine.L))
            raise
    
    def register_command(self, callback, name, nargs=None):
        def cb(state):
            cmd = name.encode(self.encoding)
            nargs_passed = lua54.lua_gettop(state)
            # print(nargs_passed)
            lua54.lua_pushlstring(state, cmd, len(cmd))
            if nargs is None:
                lua54.lua_pushinteger(state, nargs_passed)
            else:
                lua54.lua_pushinteger(state, nargs)
            self.function_to_call = callback
            return lua54.lua_yieldk(state, lua54.lua_gettop(state), 0, None)
        
        self._register(name.encode(self.encoding), cb)
        self.callbacks[name] = callback

    def __iter__(self):
        return self

c_void   = None
size_t   = ctypes.c_longlong
size_t_p = ctypes.POINTER(size_t) 

if os.name == "nt":
    lua54 = Lib(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lua", "lua54.dll"))
else:
    lua54 = Lib(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lua", "liblua.so"))

lua54.LUA_REGISTRYINDEX  = -1001000
lua54.LUA_OPEQ           = 0
lua54.LUA_OPLT           = 1
lua54.LUA_OPLE           = 2
lua54.LUA_OK             = 0
lua54.LUA_YIELD          = 1
lua54.LUA_ERRRUN         = 2
lua54.LUA_ERRSYNTAX      = 3
lua54.LUA_ERRMEM         = 4
lua54.LUA_ERRERR         = 5
lua54.LUA_TNIL           = 0
lua54.LUA_TBOOLEAN       = 1
lua54.LUA_TLIGHTUSERDATA = 2
lua54.LUA_TNUMBER        = 3
lua54.LUA_TSTRING        = 4
lua54.LUA_TTABLE         = 5
lua54.LUA_TFUNCTION      = 6
lua54.LUA_TUSERDATA      = 7
lua54.LUA_TTHREAD        = 8
lua54.LUA_NUMTYPES       = 9
lua54.lua_State_p        = ctypes.c_void_p                                    # Either an interpreter state or a thread object.
lua54.lua_CFunction      = ctypes.CFUNCTYPE(ctypes.c_int, lua54.lua_State_p)  # Pointer to a function that can be registered with lua_register

lua54.lua_yieldk       .decl(ctypes.c_int,       (lua54.lua_State_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p))
lua54.lua_settop       .decl(c_void,             (lua54.lua_State_p, ctypes.c_int))
lua54.luaL_newstate    .decl(lua54.lua_State_p,  ())
lua54.lua_pushcclosure .decl(c_void,             (lua54.lua_State_p, lua54.lua_CFunction, ctypes.c_void_p))
lua54.lua_setglobal    .decl(c_void,             (lua54.lua_State_p, ctypes.c_char_p))
lua54.lua_pushlstring  .decl(c_void,             (lua54.lua_State_p, ctypes.c_char_p, size_t))
lua54.luaL_loadstring  .decl(ctypes.c_int,       (lua54.lua_State_p, ctypes.c_char_p))
lua54.lua_pcallk       .decl(ctypes.c_int,       (lua54.lua_State_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_void_p))
lua54.lua_tolstring    .decl(ctypes.c_char_p,    (lua54.lua_State_p, ctypes.c_int, size_t_p))
lua54.lua_tonumberx    .decl(ctypes.c_double,    (lua54.lua_State_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)))
lua54.lua_getglobal    .decl(ctypes.c_int,       (lua54.lua_State_p, ctypes.c_char_p))
lua54.lua_resume       .decl(ctypes.c_int,       (lua54.lua_State_p, lua54.lua_State_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)))
lua54.lua_isstring     .decl(ctypes.c_int,       (lua54.lua_State_p, ctypes.c_int))
lua54.lua_close        .decl(c_void,             (lua54.lua_State_p, ))
lua54.lua_pushinteger  .decl(c_void,             (lua54.lua_State_p, ctypes.c_longlong))
lua54.lua_gettop       .decl(ctypes.c_int,       (lua54.lua_State_p, ))
lua54.lua_type         .decl(ctypes.c_int,       (lua54.lua_State_p, ctypes.c_int))
lua54.lua_toboolean    .decl(ctypes.c_int,       (lua54.lua_State_p, ctypes.c_int))
lua54.luaopen_base     .decl(ctypes.c_int,       (lua54.lua_State_p,))
lua54.lua_gettable     .decl(ctypes.c_int,       (lua54.lua_State_p, ctypes.c_int))
lua54.luaL_ref         .decl(ctypes.c_int,       (lua54.lua_State_p, ctypes.c_int))
lua54.lua_rawgeti      .decl(ctypes.c_int,       (lua54.lua_State_p, ctypes.c_int, ctypes.c_longlong))
lua54.lua_rawequal     .decl(ctypes.c_int,       (lua54.lua_State_p, ctypes.c_int, ctypes.c_int))
lua54.luaL_unref       .decl(c_void,             (lua54.lua_State_p, ctypes.c_int, ctypes.c_int))
lua54.lua_pushnil      .decl(c_void,             (lua54.lua_State_p,))
lua54.lua_next         .decl(ctypes.c_int,       (lua54.lua_State_p, ctypes.c_int))
lua54.lua_topointer    .decl(ctypes.c_void_p,    (lua54.lua_State_p, ctypes.c_int))
lua54.lua_pushnumber   .decl(c_void,             (lua54.lua_State_p, ctypes.c_double))
lua54.lua_pushboolean  .decl(c_void,             (lua54.lua_State_p, ctypes.c_int))
lua54.lua_compare      .decl(ctypes.c_int,       (lua54.lua_State_p, ctypes.c_int, ctypes.c_int, ctypes.c_int))
lua54.lua_newthread    .decl(lua54.lua_State_p,  (lua54.lua_State_p, ))
lua54.lua_pushthread   .decl(ctypes.c_int,       (lua54.lua_State_p, lua54.lua_State_p))
lua54.lua_xmove        .decl(c_void,             (lua54.lua_State_p, lua54.lua_State_p, ctypes.c_int))

def _lua_pop(state, n):
    lua54.lua_settop(state, -n-1)
lua54.lua_pop = _lua_pop

import asyncio

if __name__ == "__main__":
    async def lua_print(runtime, *what):
        print("[lua]", *what)

    async def lua_wait(runtime, seconds):
        await asyncio.sleep(seconds)

    TASKS = []
    async def lua_create_task(runtime, f, *args):
        for i, x in enumerate(TASKS):
            if x is None:
                TASKS[i] = asyncio.create_task(f(*args))
                return i

        TASKS.append(asyncio.create_task(f(*args)))
        return len(TASKS) - 1
    
    async def lua_join_task(runtime, i):
        i = int(i)
        result = await TASKS[i]
        TASKS[i] = None
        return result
    
    async def lua_is_task_done(runtime, i):
        return TASKS[int(i)].done()
    
    async def main1():
        with open("example.lua", "r") as f:
            program = f.read()
    
        rt = Runtime(program)
        
        rt.register_command(lua_print, "print")
        rt.register_command(lua_wait, "wait", 1)
        rt.register_command(lua_create_task, "create_task")
        rt.register_command(lua_join_task, "join_task", 1)
        rt.register_command(lua_is_task_done, "is_task_done", 1)
        
        print("Function returned:", await rt.globals()["main"]())
        print("Done!")

    async def main2():
        with open("example.lua", "r") as f:
            program = f.read()
    
        rt = Runtime(program)
        
        rt.register_command(lua_print, "print")
        rt.register_command(lua_wait, "wait", 1)
        rt.register_command(lua_create_task, "create_task")
        rt.register_command(lua_join_task, "join_task", 1)
        rt.register_command(lua_is_task_done, "is_task_done", 1)
        
        task = asyncio.create_task(rt.globals()["main"]())
        while 1:
            if task.done():
                print("Function returned:", await task)
                break
            
            print("Python mainloop")
            await asyncio.sleep(.2)
        
        print("Done!")

    async def main():
        print("Example 1")
        await main1()
        print("==============================")
        await asyncio.sleep(2)
        print("Example 2")
        await main2()
    
    asyncio.run(main())
