import sys
import subprocess
import re
import logging
import gi
import os

from typing import Callable
from gettext import gettext as _

from efiboots.efibootmgr import Efibootmgr

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gio, GObject, GLib


def btn_with_icon(icon):
    btn = Gtk.Button(hexpand=True)
    icon = Gio.ThemedIcon(name=icon)
    image = Gtk.Image.new_from_gicon(icon)
    btn.set_child(image)
    return btn


def yes_no_dialog(parent, primary, secondary, on_response):
    dialog = Gtk.MessageDialog(transient_for=parent, message_type=Gtk.MessageType.QUESTION,
                               buttons=Gtk.ButtonsType.YES_NO, text=primary, secondary_text=secondary)
    area = dialog.get_message_area()
    child = area.get_first_child()
    while child:
        child.set_selectable(True)
        child = child.get_next_sibling()
    dialog.connect('response', on_response)
    dialog.show()
    return dialog


def error_dialog(transient_for: Gtk.Window, message: str, title: str, on_response: callable):
    dialog = Gtk.MessageDialog(transient_for=transient_for, destroy_with_parent=True,
                               message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                               text=title, secondary_text=message, modal=True)
    area = dialog.get_message_area()
    child = area.get_first_child()
    while child:
        child.set_selectable(True)
        child = child.get_next_sibling()
    dialog.connect('response', on_response)
    dialog.show()
    return dialog


many_esps_error_message = _("""
This program detected more than one EFI System Partition on your system. You have to choose the right one.
You can either mount your ESP on /boot/efi or pass the ESP block device via --disk and --part
(e.g. --disk=/dev/sda --part=1).

Choose wisely.
""")

device_regex = re.compile(r'^([a-z/]+[0-9a-z]*?)p?([0-9]+)$')


def is_in_flatpak():
    return "FLATPAK_ID" in os.environ


def subprocess_run_wrapper(cmd):
    if is_in_flatpak():
        cmd = [ "flatpak-spawn", "--host" ] + cmd
        logging.debug("Flatpak sandbox detected. Running: %s", ' '.join(cmd))
    else:
        logging.debug("Running: %s", ' '.join(cmd))
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def device_to_disk_part(device: str) -> tuple[str, str]:
    try:
        disk, part = device_regex.match(device).groups()
        logging.debug("Device path %s split into %s and %s", device, disk, part)
        return disk, part
    except AttributeError:
        raise ValueError("Could not match device " + device)


def make_auto_detect_esp_with_findmnt(esp_mount_point) -> Callable:
    def auto_detect_esp_with_findmnt() -> tuple[str, str] | None:
        # findmnt --noheadings --output SOURCE --mountpoint /boot/efi
        cmd = ["findmnt", "--noheadings", "--output", "SOURCE,FSTYPE", "--mountpoint", esp_mount_point]


        try:
            findmnt_output = subprocess_run_wrapper(cmd)
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            logging.warning("Could not detect ESP with findmnt: %s", e)
            return
        splitted = findmnt_output.strip().split()
        for source, fstype in zip(splitted[::2], splitted[1::2]):
            if fstype == 'vfat':
                disk, part = device_to_disk_part(source)
                return disk, part

    return auto_detect_esp_with_findmnt


def auto_detect_esp_with_lsblk() -> tuple[str, str] | None:
    """
    Finds the ESP by scanning the partition table. It should work with GPT (tested) and MBR (not tested).
    This method doesn't require the ESP to be mounted.
    :return: 2 strings that can be passed to efibootmgr --disk and --part argument.
    """
    esp_part_types = ('C12A7328-F81F-11D2-BA4B-00A0C93EC93B', 'EF')

    # lsblk --noheadings --pairs --paths --output NAME,PARTTYPE
    cmd = ['lsblk', '--noheadings', '--pairs', '--paths', '--output', 'NAME,PARTTYPE,FSTYPE']

    logging.debug("Running: %s", ' '.join(cmd))
    try:
        res = subprocess_run_wrapper(cmd).strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        logging.warning("Could not detect ESP with lsblk: %s", e)
        return
    regex = re.compile('^NAME="(.+)" PARTTYPE="(.+)" FSTYPE="(.+)"$', re.MULTILINE)
    esps = []
    for match in regex.finditer(res):
        name, part_type, fs_type = match.groups()
        if part_type.upper() in esp_part_types and fs_type == 'vfat':
            esps.append(name)
    logging.info(esps)
    if len(esps) == 0:
        return None
    if len(esps) == 1:
        source = esps[0]
        disk, part = device_to_disk_part(source)
    else:
        logging.warning(many_esps_error_message)
        def on_response(*args):
            logging.debug("sys.exit(-1)")
            sys.exit(-1)
        error_dialog(None, many_esps_error_message + "\n" + _("Detected ESPs: ") + ', '.join(esps),
                     _("More than one EFI System Partition detected!"), on_response)
        return None
    return disk, part


def auto_detect_esp():
    methods = (make_auto_detect_esp_with_findmnt('/efi'), make_auto_detect_esp_with_findmnt('/boot/efi'),
               make_auto_detect_esp_with_findmnt('/boot'), auto_detect_esp_with_lsblk)
    for find_esp_method in methods:
        result = find_esp_method()
        if not result:
            continue
        disk, part = result
        logging.info("Detected ESP on disk %s part %s", disk, part)
        return disk, part
    logging.fatal("Can't auto-detect ESP! All methods failed.")
    return None, None


def execute_script_as_root(script):
    logging.info("Running command `pkexec sh -c %s`", script)
    subprocess_run_wrapper(["pkexec", "sh", "-c", script])


class EfibootRowModel(GObject.Object):
    __gtype_name__ = "EfibootRowModel"

    current = GObject.Property(type=bool, default=False)
    num = GObject.Property(type=str)
    name = GObject.Property(type=str)
    path = GObject.Property(type=str)
    parameters = GObject.Property(type=str)
    active = GObject.Property(type=bool, default=True)
    next = GObject.Property(type=bool, default=False)

    def __init__(self, current: bool, num: str, name: str, path: str, parameters: str, active: bool, next: bool):
        super().__init__()
        self.current = current
        self.num = num
        self.name = name
        self.path = path
        self.parameters = parameters
        self.active = active
        self.next = next

        self.radio_buttons_group = Gtk.CheckButton()

    def __str__(self):
        return f"EfibootModelRow {'current' if self.current else ''} num{self.num} {self.name} {self.path}" \
               f" {self.parameters} {'active' if self.active else 'inactive'} {'next' if self.next else ''}"


class EfibootsListStore(Gio.ListStore):
    def __init__(self, window: 'EfibootsMainWindow'):
        self.window = window
        super().__init__(item_type=EfibootRowModel)
        self._efibootmgr = None

        self.boot_order = []
        self.boot_order_initial = []
        self.boot_next = None
        self.boot_next_initial = None
        self.boot_active = set()
        self.boot_inactive = set()
        self.boot_add = {}
        self.boot_remove = set()
        self.boot_current = None
        self.timeout = None
        self.timeout_initial = None
        self.edit_parameters = set()
        self.edit_loader = set()
        self.edit_name = set()

    def __str__(self):
        return f"next: {self.boot_next} order: {self.boot_order} add: {self.boot_add} rem: {self.boot_remove} " \
               f"active: {self.boot_active} inactive: {self.boot_inactive} timeout: {self.timeout}"

    @property
    def efibootmgr(self):
        if self._efibootmgr is None:
            self._efibootmgr = Efibootmgr.get_instance()
        return self._efibootmgr

    def swap(self, a, b):
        self.boot_order[a], self.boot_order[b] = self.boot_order[b], self.boot_order[a]

    def index_num(self, num):
        for i, row in enumerate(self):
            if row.num == num:
                return i

    def clear(self):
        self.remove_all()
        self.boot_order = []
        self.boot_order_initial = []
        self.boot_next: str | None = None
        self.boot_next_initial: str | None = None
        self.boot_active = set()
        self.boot_inactive = set()
        self.boot_add = {}
        self.boot_remove = set()
        self.boot_current = None
        self.timeout = None
        self.timeout_initial = None
        self.edit_parameters = set()
        self.edit_loader = set()
        self.edit_name = set()

    def refresh(self):
        self.clear()

        try:
            boot = self.efibootmgr.run()
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            logging.exception("Error running efibootmgr. Please check that it is correctly installed.")
            error_dialog(transient_for=self.window, title=_("efibootmgr utility not installed!"),
                         message=_("Please check that the efibootmgr utility is correctly installed, as this program requires its output.") + f"\n{str(e)}",
                         on_response=lambda *_: sys.exit(-1))
            return
        except UnicodeDecodeError as e:
            logging.exception("Error decoding efibootmgr -v output.")
            error_dialog(transient_for=self.window, title=_("Error while decoding efibootmgr output."),
                         message=_("Could not decode efiboomgr output.") + f"\n{e}", on_response=lambda *_: sys.exit(-2))
            return

        if boot is not None:
            parsed_efi = self.efibootmgr.parse(boot)
            for entry in parsed_efi.entries:
                row = EfibootRowModel(entry.num == parsed_efi.boot_current,
                                      entry.num,
                                      entry.name,
                                      entry.path,
                                      entry.parameters,
                                      entry.active,
                                      entry.num == parsed_efi.boot_next)
                self.append(row)

            self.boot_order_initial = parsed_efi.boot_order
            self.boot_order = list(self.boot_order_initial)
            self.boot_next = self.boot_next_initial = parsed_efi.boot_next
            self.boot_current = parsed_efi.boot_current
            self.timeout = self.timeout_initial = parsed_efi.timeout
            self.window.timeout_spin.set_value(self.timeout)

            self.sort(self.sort_by_boot_order)

    def sort_by_boot_order(self, row1: EfibootRowModel, row2: EfibootRowModel) -> int:
        row1_index = self.boot_order.index(row1.num)
        row2_index = self.boot_order.index(row2.num)
        return row1_index - row2_index

    def change_boot_next(self, action: Gio.SimpleAction, num_variant: GLib.Variant):
        num = num_variant.get_string()
        if self.boot_next == num:
            action.set_state(GLib.Variant.new_string(""))
            self.boot_next = None
        else:
            action.set_state(num_variant)
            self.boot_next = num
        logging.debug("%s changed to %s", action.get_name(), action.get_state())

    def change_active(self, widget: Gtk.Switch, state: bool, row: EfibootRowModel):
        row.active = state
        num = row.num
        if row.active:
            if num in self.boot_inactive:
                self.boot_inactive.remove(num)
            else:
                self.boot_active.add(num)
        else:
            if num in self.boot_active:
                self.boot_active.remove(num)
            else:
                self.boot_inactive.add(num)

        logging.debug("%s %s %s", row, self.boot_active, self.boot_inactive)

    def add(self, label, path, parameters):
        new_num = "NEW{:d}".format(len(self.boot_add))
        row = EfibootRowModel(False, new_num, label, path, parameters, True, False)
        self.append(row)
        self.boot_add[new_num] = (label, path, parameters)

    def remove(self, position: int):
        item: EfibootRowModel | None = self.get_item(position)
        if item is not None:
            num: str = item.num
            if num.startswith('NEW'):
                del self.boot_add[num]
            else:
                self.boot_remove.add(num)
                self.boot_order.remove(num)
            super().remove(position)

    def pending_changes(self):
        logging.debug("%s", self)
        return (self.boot_next_initial != self.boot_next or
                self.boot_order_initial != self.boot_order or self.boot_add or
                self.boot_remove or self.boot_active or self.boot_inactive
                or self.timeout != self.timeout_initial
                )

    def to_script(self, disk, part, reboot):
        esp = f"--disk {disk} --part {part}"
        script = ''
        for entry in self.boot_remove:
            script += f'efibootmgr {esp} --delete-bootnum --bootnum {entry}\n'
        for label, loader, params in self.boot_add.values():
            script += f'efibootmgr {esp} --create --label \'{label}\' --loader \'{loader}\' --unicode \'{params}\'\n'
        if self.boot_order != self.boot_order_initial:
            script += f'efibootmgr {esp} --bootorder {",".join(self.boot_order)}\n'
        if self.boot_next_initial != self.boot_next:
            if self.boot_next is None:
                script += f'efibootmgr {esp} --delete-bootnext\n'
            else:
                script += f'efibootmgr {esp} --bootnext {self.boot_next}\n'
        for entry in self.boot_active:
            script += f'efibootmgr {esp} --bootnum {entry} --active\n'
        for entry in self.boot_inactive:
            script += f'efibootmgr {esp} --bootnum {entry} --inactive\n'
        if self.timeout != self.timeout_initial:
            script += f'efibootmgr {esp} --timeout {self.timeout}\n'
        if reboot:
            script += "reboot\n"
        return script


@Gtk.Template(resource_path='/ovh/elinvention/Efiboots/gtk/main.ui')
class EfibootsMainWindow(Gtk.ApplicationWindow):
    __gtype_name__ = "EfibootsMainWindow"

    column_view: Gtk.ColumnView = Gtk.Template.Child()
    column_next: Gtk.ColumnViewColumn = Gtk.Template.Child()
    column_active: Gtk.ColumnViewColumn = Gtk.Template.Child()

    up: Gtk.Button = Gtk.Template.Child()
    down: Gtk.Button = Gtk.Template.Child()
    add: Gtk.Button = Gtk.Template.Child()
    remove: Gtk.Button = Gtk.Template.Child()

    timeout_spin: Gtk.SpinButton = Gtk.Template.Child()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.APP_VERSION: str = kwargs['application'].APP_VERSION
        self.part: str | None = None
        self.disk: str | None = None
        self.model = EfibootsListStore(self)
        self.selection_model = Gtk.SingleSelection(model=self.model)
        self.column_view.set_model(self.selection_model)
        self.timeout_spin.set_adjustment(Gtk.Adjustment(lower=0, step_increment=1, upper=999))

        def on_setup_active(_: Gtk.ListItemFactory, item: Gtk.ListItem):
            switch = Gtk.Switch()
            item.set_child(switch)

        def on_bind_active(_: Gtk.ListItemFactory, item: Gtk.ListItem):
            row: EfibootRowModel = item.get_item()
            switch: Gtk.Switch = item.get_child()
            switch.set_active(row.active)
            switch._binding = switch.connect("state-set", self.model.change_active, row)

        def on_unbind_active(_: Gtk.ListItemFactory, item: Gtk.ListItem):
            switch: Gtk.Switch = item.get_child()
            if switch._binding:
                switch.disconnect(switch._binding)
                switch._binding = None

        def on_teardown_active(_: Gtk.ListItemFactory, item: Gtk.ListItem):
            switch: Gtk.Switch = item.get_child()
            if switch and switch._binding:
                switch._binding = None

        factory_active = Gtk.SignalListItemFactory.new()
        factory_active.connect("setup", on_setup_active)
        factory_active.connect("bind", on_bind_active)
        factory_active.connect("unbind", on_unbind_active)
        factory_active.connect("teardown", on_teardown_active)
        self.column_active.set_factory(factory_active)

        var = GLib.Variant.new_string("")
        action_next = Gio.SimpleAction.new_stateful("next_boot", var.get_type(), var)
        action_next.connect("change-state", self.model.change_boot_next)
        self.add_action(action_next)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self.on_activate_about)
        self.add_action(about_action)

        def on_setup_next_boot(_: Gtk.ListItemFactory, item: Gtk.ListItem):
            num_variant = GLib.Variant.new_string("0000")  # set to something not None
            checkbutton = Gtk.CheckButton(action_name="win.next_boot", action_target=num_variant)
            item.set_child(checkbutton)

        def on_bind_next_boot(_: Gtk.ListItemFactory, item: Gtk.ListItem):
            row: EfibootRowModel = item.get_item()
            checkbutton: Gtk.CheckButton = item.get_child()
            checkbutton._binding = checkbutton.bind_property("active", row, "next", GObject.BindingFlags.BIDIRECTIONAL)
            checkbutton.set_action_target_value(GLib.Variant.new_string(row.num))

        def on_unbind_next_boot(_: Gtk.ListItemFactory, item: Gtk.ListItem):
            checkbutton: Gtk.CheckButton = item.get_child()
            if checkbutton._binding:
                checkbutton._binding.unbind()
                checkbutton._binding = None

        def on_teardown_next_boot(_: Gtk.ListItemFactory, item: Gtk.ListItem):
            checkbutton: Gtk.CheckButton = item.get_child()
            if checkbutton and checkbutton._binding:
                checkbutton._binding = None

        factory = Gtk.SignalListItemFactory.new()
        factory.connect("setup", on_setup_next_boot)
        factory.connect("bind", on_bind_next_boot)
        factory.connect("unbind", on_unbind_next_boot)
        factory.connect("teardown", on_teardown_next_boot)
        self.column_next.set_factory(factory)

        self.add_css_class("devel")

    def on_activate_about(self, action, param):
        logging.debug("on_activate_about")
        about_builder = Gtk.Builder.new_from_resource("/ovh/elinvention/Efiboots/gtk/about.ui")
        about_dialog: Gtk.AboutDialog = about_builder.get_object("about_dialog")
        about_dialog.set_version(self.APP_VERSION)
        about_dialog.set_transient_for(self)
        about_dialog.present()

    def next_boot_handler(self, action: Gio.SimpleAction, state: str):
        self.model.boot_next = state

    def query_system(self, disk, part):
        if not (disk and part):
            disk, part = auto_detect_esp()
        if not (disk and part):
            error_dialog(self, _("Could not find an EFI System Partition. Ensure your ESP is mounted on /efi, "
                               "/boot/efi or /boot, that it has the correct partition type and vfat file system and that "
                               "either findmnt or lsblk commands are installed (should be by default on most distros)."),
                         _("Can't auto-detect ESP!"), lambda *_: sys.exit(-1))
            return
        self.disk, self.part = disk, part
        self.model.refresh()

    @Gtk.Template.Callback()
    def on_clicked_up(self, _: Gtk.Button):
        index = self.selection_model.get_selected()
        self.model.swap(index, max(index - 1, 0))
        self.model.sort(self.model.sort_by_boot_order)

    @Gtk.Template.Callback()
    def on_clicked_down(self, _: Gtk.Button):
        index = self.selection_model.get_selected()
        self.model.swap(index, min(index + 1, len(self.model) - 1))
        self.model.sort(self.model.sort_by_boot_order)

    @Gtk.Template.Callback()
    def on_clicked_add(self, __: Gtk.Button):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   destroy_with_parent=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.OK_CANCEL,
                                   text=_("Label is mandatory. It is the name that will show up in your EFI boot menu.\n\n"
                                        "Path is the path to the loader relative to the ESP, like \\EFI\\Boot\\bootx64.efi\n\n"
                                        "Parameters is an optional list of aguments to pass to the loader (your kernel parameters if you use EFISTUB)"))

        dialog.set_title(_("New EFI loader"))
        yes_button = dialog.get_widget_for_response(Gtk.ResponseType.OK)
        yes_button.set_sensitive(False)
        dialog_box = dialog.get_content_area()

        fields = ["label", "path", "parameters"]
        entries = {}
        grid = Gtk.Grid(row_spacing=2, column_spacing=8, halign=Gtk.Align.CENTER)
        for i, field in enumerate(fields):
            entries[field] = Gtk.Entry()
            entries[field].set_size_request(400, 0)
            label = Gtk.Label(label=field.capitalize() + ":")
            grid.attach(label, 0, i, 1, 1)
            grid.attach(entries[field], 1, i, 1, 1)

        dialog_box.append(grid)
        entries["label"].connect('changed', lambda l: yes_button.set_sensitive(l.get_text() != ''))

        def on_response(add_dialog, response):
            new_label, path, parameters = map(lambda e_field: entries[e_field].get_text(), fields)
            if response == Gtk.ResponseType.OK:
                self.model.add(new_label, path, parameters)
                self.remove.set_sensitive(True)
            add_dialog.close()

        dialog.connect('response', on_response)
        dialog.show()

    @Gtk.Template.Callback()
    def on_clicked_duplicate(self, __: Gtk.Button):
        row: EfibootRowModel | None = self.selection_model.get_selected_item()
        if row:
            self.model.add(_("Copy of ") + row.name, row.path, row.parameters)

    @Gtk.Template.Callback()
    def on_clicked_remove(self, button: Gtk.Button):
        row = self.selection_model.get_selected_item()
        index = self.selection_model.get_selected()
        logging.debug(f"Removing {row} at {index}")
        self.model.remove(index)
        if len(self.model) == 0:
            button.set_sensitive(False)

    @Gtk.Template.Callback()
    def on_clicked_save(self, button: Gtk.Button):
        if self.model.pending_changes():
            script = self.model.to_script(self.disk, self.part, button.get_buildable_id() == "reboot_button")

            def on_response(dialog, response):
                if response == Gtk.ResponseType.YES:
                    try:
                        execute_script_as_root(script)
                        self.model.refresh()
                    except FileNotFoundError as e:
                        error_dialog(self, _("The pkexec command from PolKit is "
                                           "required to execute commands with elevated privileges.\n") +
                                           f"{e}", _("pkexec not found"), lambda d, r: d.close())
                    except subprocess.CalledProcessError as e:
                        error_dialog(self, f"{e}\n{e.stderr.decode()}", "Error", lambda d, r: d.close())
                dialog.close()

            yes_no_dialog(self, _("Are you sure you want to continue?"),
                          _("Your changes are about to be written to EFI NVRAM.") + "\n" +
                          _("The following commands will be run:") + "\n\n" + script,
                          on_response)

    @Gtk.Template.Callback()
    def on_clicked_reboot(self, button: Gtk.Button):
        if self.model.pending_changes():
            self.on_clicked_save(button)
        else:
            def on_response(response_dialog, response):
                if response == Gtk.ResponseType.YES:
                    try:
                        execute_script_as_root("reboot\n")
                    except FileNotFoundError as e:
                        error_dialog(self, _("The pkexec command from PolKit is "
                                           "required to execute commands with elevated privileges.")
                                           + f"\n{e}", _("pkexec not found"), lambda d, r: d.close())
                    except subprocess.CalledProcessError as e:
                        error_dialog(self, f"{e}\n{e.stderr.decode()}", "Error", lambda d, r: d.close())
                response_dialog.close()

            yes_no_dialog(self, _("Are you sure you want to reboot?"),
                          _("Press OK to reboot your computer."),
                          on_response)

    def discard_warning(self, on_response, win: Gtk.Window):
        if self.model.pending_changes():
            return yes_no_dialog(win, _("Are you sure you want to discard?"),
                                 _("Your changes will be lost if you don't save them."),
                                 on_response)
        else:
            return None

    @Gtk.Template.Callback()
    def on_clicked_reset(self, _: Gtk.Button):
        def on_response(dialog, response):
            if response == Gtk.ResponseType.YES:
                self.model.refresh()
            dialog.close()

        if not self.discard_warning(on_response, self):
            self.model.refresh()

    @Gtk.Template.Callback()
    def on_clicked_about(self, _: Gtk.Button):
        logging.debug("about clicked")
        self.activate_action("win.about")

    @Gtk.Template.Callback()
    def on_value_changed_timeout(self, spin: Gtk.SpinButton):
        self.model.timeout = spin.get_value_as_int()

    # @Gtk.Template.Callback()
    # def on_toggled_active(self, check: Gtk.CheckButton, checked_row: EfibootRowModel):
    #    print(check, checked_row)
    #    self.model.change_active(check, checked_row)

    @Gtk.Template.Callback()
    def on_close_request(self, win: Gtk.ApplicationWindow):
        def on_response(dialog, response):
            dialog.close()
            if response == Gtk.ResponseType.YES:
                win.destroy()

        if self.discard_warning(on_response, win):
            return True

    @Gtk.Template.Callback()
    def on_query_tooltip(self, column_view: Gtk.ColumnView, x: int, y: int, keyboard_mode: bool, tooltip: Gtk.Tooltip):
        if keyboard_mode:
            print("keyboard")
            cursor = column_view.get_cursor()
            if not cursor:
                return False
        else:
            # print("mouse", x, y)
            # new_x, new_y = self.translate_coordinates(column_view, x, y)
            # print("new", new_x, new_y)
            pass

        tooltip.set_text("test")
        # column_view.set_tooltip_text("test")
        return True


