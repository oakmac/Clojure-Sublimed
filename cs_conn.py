import os, re, sublime, sublime_plugin, threading, time
from . import cs_common, cs_eval, cs_eval_status, cs_parser, cs_warn

status_key = 'clojure-sublimed-conn'
phases = ['🌑', '🌒', '🌓', '🌔', '🌕']

def ready(window = None):
    """
    When connection is fully initialized
    """
    state = cs_common.get_state(window)
    return bool(state.conn and state.conn.ready())

class Connection:
    def __init__(self):
        self.status = None
        self.disconnecting = False
        self.window = sublime.active_window()

    def get_addr(self):
        return self.addr() if callable(self.addr) else self.addr

    def connect_impl(self):
        pass

    def connect(self):
        """
        Connect to address specified during construction
        """
        state = cs_common.get_state()
        try:
            self.connect_impl()
            state.conn = self
        except Exception as e:
            cs_common.error('Connection failed')
            self.disconnect()
            if window := sublime.active_window():
                window.status_message(f'Connection failed')

    def try_connect_impl(self, timeout):
        state = cs_common.get_state(self.window)
        t0 = time.time()
        attempt = 1
        while time.time() - t0 <= timeout:
            time.sleep(0.25)
            try:
                cs_common.debug('Connection attempt #{} to {}', attempt, self.get_addr())
                self.connect_impl()
                state.conn = self
                return
            except Exception as e:
                attempt += 1
        cs_common.error('Giving up after {} sec connecting to {}', round(time.time() - t0, 2), self.get_addr())
        self.disconnect()
        if window := sublime.active_window():
            window.status_message(f'Connection failed')

    def try_connect(self, timeout = 0):
        state = cs_common.get_state(self.window)
        if timeout:
            threading.Thread(target = self.try_connect_impl, args=(timeout,)).start()
        else:
            self.connect()

    def ready(self):
        return bool(self.status and self.status[0] == phases[4])

    def eval_impl(self, form):
        pass

    def eval_region(self, region, view):
        if region.empty():
            if eval := cs_eval.by_region(view, region):
                return eval.region()
            return cs_parser.topmost_form(view, region.begin())
        return region

    def code(self, view, selected_region, eval_region, transform_fn = None):
        code = view.substr(eval_region)
        ns = cs_parser.namespace(view, eval_region.begin()) or 'user'
        parsed = cs_parser.parse(view.substr(eval_region))
        forms = [child for child in parsed.children if child.name not in {'comment', 'discard'}]
        
        if transform_fn:
            symbol = cs_parser.defsym(forms[0]) if len(forms) == 1 else None
            kwargs = {'selected_region': selected_region,
                      'eval_region':     eval_region,
                      'ns':              ns,
                      'symbol':          symbol}
            code = transform_fn(code, **kwargs)

        return (code, ns, forms)


    def eval(self, view, sel, transform_fn = None, print_quota = None, on_finish = None):
        """
        Eval code and call `cs_eval.on_success(id, value)` or `cs_eval.on_exception(id, value, trace)`
        """
        for selected_region in sel:
            eval_region = self.eval_region(selected_region, view)
            eval = cs_eval.Eval(view, eval_region, on_finish = on_finish)
            (line, column) = view.rowcol_utf16(eval_region.begin())
            line = line + 1

            (code, ns, forms) = self.code(view, selected_region, eval_region, transform_fn)

            form = cs_common.Form(
                    id     = eval.id,
                    code   = code,
                    ns     = ns,
                    line   = line,
                    column = column,
                    file   = view.file_name(),
                    print_quota = print_quota)
            self.eval_impl(form)

    def eval_status(self, code, ns):
        eval = cs_eval_status.StatusEval(code)
        form = cs_common.Form(id = eval.id, code = code, ns = ns)
        self.eval_impl(form)

    def load_file_impl(self, id, file, path):
        pass

    def load_file(self, view):
        """
        Load whole file (~load-file nREPL command). Same callbacks as `eval`
        """
        region = sublime.Region(0, view.size())
        eval = cs_eval.Eval(view, region)
        self.load_file_impl(eval.id, view.substr(region), view.file_name())

    def lookup_impl(self, id, symbol, ns):
        pass

    def lookup(self, view, region):
        """
        Look symbol up and call `cs_eval.on_lookup(id, value)`
        """
        symbol = view.substr(region)
        ns     = cs_parser.namespace(view, region.begin()) or 'user'
        eval   = cs_eval.Eval(view, region)
        self.lookup_impl(eval.id, symbol, ns)

    def interrupt_impl(self, batch_id, id):
        pass

    def interrupt(self, batch_id, id):
        """
        Interrupt currently executing eval with id = id.
        Will probably call `cs_eval.on_exception(id, value, trace)` on interruption
        """
        self.interrupt_impl(batch_id, id)

    def disconnect_impl(self):
        pass

    def disconnect(self):
        """
        Disconnect from REPL
        """
        if self.disconnecting:
            return
        self.disconnecting = True
        self.disconnect_impl()
        state = cs_common.get_state()
        state.conn = None
        cs_common.set_status(self.window, status_key, None)
        cs_eval.erase_evals(lambda eval: eval.window == self.window)
        cs_warn.reset_warnings(self.window)

    def set_status(self, phase, message, *args):
        status = phases[phase] + ' ' + message.format(*args)
        self.status = status
        cs_common.set_status(self.window, status_key, status)

class AddressInputHandler(sublime_plugin.TextInputHandler):
    def __init__(self, port_files = [], next_input = None):
        self.port_files = port_files
        self.next = next_input

    """
    Reusable InputHandler that remembers last address and can also look for .nrepl-port file
    """
    def placeholder(self):
        return "host:port or /path/to/nrepl.sock"

    def initial_text(self):
        # .nrepl-port file present
        if self.port_files and (window := sublime.active_window()):
            for folder in window.folders():
                for port_file in self.port_files:
                    if os.path.exists(folder + "/" + port_file):
                        with open(folder + "/" + port_file, "rt") as f:
                            content = f.read(10).strip()
                            if re.fullmatch(r'[1-9][0-9]*', content):
                                return f'localhost:{content}'
        state = cs_common.get_state()
        return state.last_conn[1]['address'] if state.last_conn else 'localhost:'

    def initial_selection(self):
        text = self.initial_text()
        end = len(text)
        if ':' in text:
            return [(text.rfind(':') + 1, end)]
        elif '/' in text:
            return [(text.rfind('/') + 1, end)]

    def preview(self, text):
        if not self.validate(text):
            return 'Expected <host>:<port> or <path>'

    def validate(self, text):
        text = text.strip()
        if 'auto' == text:
            return True
        elif match := re.fullmatch(r'([a-zA-Z0-9\.]+):(\d{1,5})', text):
            _, port = match.groups()
            return 1 <= int(port) and int(port) < 65536
        else:
            return os.path.isfile(text)

    def next_input(self, args):
        return self.next

class ClojureSublimedReconnectCommand(sublime_plugin.WindowCommand):
    def run(self):
        state = cs_common.get_state(self.window)
        if state.conn:
            self.window.run_command('clojure_sublimed_disconnect', {})
        self.window.run_command(*state.last_conn)

    def is_enabled(self):
        state = cs_common.get_state(self.window)
        return state.last_conn is not None

class ClojureSublimedDisconnectCommand(sublime_plugin.WindowCommand):
    def run(self):
        state = cs_common.get_state(self.window)
        state.conn.disconnect()

    def is_enabled(self):
        state = cs_common.get_state(self.window)
        return state.conn is not None

def plugin_unloaded():
    for state in cs_common.states.values():
        if state.conn:
            state.conn.disconnect()
