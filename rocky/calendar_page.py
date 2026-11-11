"""Calendar month grid and inline event editor embedded in Rocky's sidebar pages."""
import calendar
from datetime import datetime,timedelta,timezone
import sqlite3
import tkinter as tk
from tkinter import ttk,messagebox
from tkinter import font as tkfont
from zoneinfo import ZoneInfo

from rocky.calendar_model import month_days,month_shift,view_bounds,schedule_events,group_days,load_calendar
from rocky.calendar_ui import event_time,display_time,DATE_FORMAT
from rocky.recurrence import REPEATS
from rocky.storage import utc_now

BG='#11111f'
CARD='#191929'
CELL='#202034'
TEXT='#f4f2fc'
MUTED='#b9b7d2'
MINT='#72eac8'
VIOLET='#a98aff'


class CalendarPage(tk.Frame):
    def __init__(self,parent,store,zone='Asia/Kolkata',startup=None,on_notify=None,on_start=None,on_pause=None,service_status=None):
        super().__init__(parent,background=BG,padx=16,pady=14)
        self.root=self.winfo_toplevel()
        self.store,self.zone,self.startup=store,zone,startup
        self.on_notify=on_notify or (lambda message:None)
        self.on_start,self.on_pause,self.service_status=on_start,on_pause,service_status
        today=utc_now().astimezone(ZoneInfo(zone)).date()
        self.year,self.month=today.year,today.month
        self.selected_day=today
        self.editor_row=None
        self.current_key=None
        self.grouped={}
        self.events_by_key={}
        self.signature=None
        self.narrow=True
        self.status=tk.StringVar(value='Choose a day to add an event, or select an event to edit it.')
        self.month_title=tk.StringVar()
        self.day_title=tk.StringVar()
        self.mode=tk.StringVar(value='New event')
        self.alert_status=tk.StringVar()
        self.body=tk.StringVar()
        self.date=tk.StringVar()
        self.clock=tk.StringVar()
        self.event_zone=tk.StringVar(value=zone)
        self.initial_form=None
        self.day_font=tkfont.Font(self,family='Segoe UI',size=11,weight='bold')
        self.event_font=tkfont.Font(self,family='Segoe UI',size=9)
        self.week_font=tkfont.Font(self,family='Segoe UI',size=9,weight='bold')
        style=ttk.Style(self)
        style.configure('RockyCalendar.Treeview',background=CARD,fieldbackground=CARD,foreground=TEXT,
                        rowheight=28,font=('Segoe UI',10),borderwidth=0)
        style.configure('RockyCalendar.Treeview.Heading',background='#303046',foreground=TEXT,font=('Segoe UI',9,'bold'))
        style.map('RockyCalendar.Treeview',background=[('selected','#4c3676')],foreground=[('selected',TEXT)])
        header=ttk.Frame(self)
        header.pack(fill='x')
        ttk.Label(header,text='Calendar',font=('Segoe UI',24,'bold')).pack(side='left')
        ttk.Button(header,text='New event',style='Accent.TButton',command=self.new_event).pack(side='right')
        toolbar=ttk.Frame(self)
        toolbar.pack(fill='x',pady=(12,10))
        ttk.Button(toolbar,text='‹',width=3,command=lambda:self.change_month(-1)).pack(side='left')
        ttk.Button(toolbar,text='›',width=3,command=lambda:self.change_month(1)).pack(side='left',padx=4)
        ttk.Button(toolbar,text='Today',command=self.go_today).pack(side='left')
        ttk.Label(toolbar,textvariable=self.month_title,font=('Segoe UI',15,'bold')).pack(side='left',padx=12)
        self.view_zone=ttk.Combobox(toolbar,values=('Asia/Kolkata','UTC','America/New_York','Europe/London','Asia/Dubai','Asia/Singapore'),width=18)
        self.view_zone.set(zone)
        self.view_zone.pack(side='right')
        self.view_zone.bind('<<ComboboxSelected>>',self.change_zone)
        self.view_zone.bind('<Return>',self.change_zone)
        self.viewport=tk.Canvas(self,background=BG,highlightthickness=0)
        scroll=ttk.Scrollbar(self,orient='vertical',command=self.viewport.yview)
        scroll.pack(side='right',fill='y')
        self.viewport.pack(fill='both',expand=True)
        self.viewport.configure(yscrollcommand=scroll.set)
        self.content=ttk.Frame(self.viewport)
        self.content_window=self.viewport.create_window(0,0,anchor='nw',window=self.content)
        self.month_frame=ttk.Frame(self.content)
        self.month_frame.grid(row=0,column=0,sticky='nsew')
        self.grid_canvas=tk.Canvas(self.month_frame,background=BG,highlightthickness=0,height=440)
        self.grid_canvas.pack(fill='both',expand=True)
        self.grid_canvas.bind('<Configure>',lambda event:self.draw_month())
        self.grid_canvas.bind('<Button-1>',self.grid_click)
        self.inspector=ttk.Frame(self.content,padding=(12,4,4,0))
        self.inspector.grid(row=1,column=0,sticky='nsew')
        self.tabs=ttk.Notebook(self.inspector)
        self.tabs.pack(fill='both',expand=True)
        self.edit_tab=ttk.Frame(self.tabs,padding=10)
        self.history_tab=ttk.Frame(self.tabs,padding=10)
        self.tabs.add(self.edit_tab,text='Day & event')
        self.tabs.add(self.history_tab,text='Recent alerts')
        ttk.Label(self.edit_tab,textvariable=self.day_title,font=('Segoe UI',13,'bold')).pack(anchor='w')
        ttk.Button(self.edit_tab,text='Back to month',command=lambda:self.viewport.yview_moveto(0)).pack(anchor='w',pady=(4,8))
        self.day_list=self.table(self.edit_tab,('time','event'),('Time','Event'),height=4)
        self.day_list.bind('<<TreeviewSelect>>',self.list_select)
        ttk.Label(self.edit_tab,textvariable=self.mode,wraplength=300,font=('Segoe UI',10,'bold')).pack(anchor='w',pady=(12,4))
        self.fields=[]
        for label,variable in [('Event name',self.body),('Date (YYYY-MM-DD)',self.date),('Time (24-hour HH:MM)',self.clock),('Event time zone',self.event_zone)]:
            ttk.Label(self.edit_tab,text=label).pack(anchor='w')
            entry=ttk.Entry(self.edit_tab,textvariable=variable)
            entry.pack(fill='x',pady=(2,6))
            self.fields.append(entry)
        ttk.Label(self.edit_tab,text='Repeat').pack(anchor='w')
        self.repeat_picker=ttk.Combobox(self.edit_tab,state='readonly',values=list(REPEATS.values()))
        self.repeat_picker.pack(fill='x',pady=(2,8))
        commands=ttk.Frame(self.edit_tab)
        commands.pack(fill='x')
        self.save_button=ttk.Button(commands,text='Save event',command=self.save_event,style='Accent.TButton')
        self.save_button.pack(side='left')
        self.cancel_button=ttk.Button(commands,text='Cancel event',command=self.cancel_event)
        self.cancel_button.pack(side='left',padx=5)
        self.done_button=ttk.Button(self.edit_tab,text='Mark done — stop repeat alerts',command=self.complete_event)
        self.done_button.pack(anchor='w',pady=(8,0))
        self.history_list=self.table(self.history_tab,('time','event'),('Alert time','Event'),height=10)
        self.history_list.column('time',width=145,minwidth=120)
        self.history_rows={}
        self.snooze_picker=ttk.Combobox(self.history_tab,state='readonly',values=('5 minutes','15 minutes','30 minutes'),width=14)
        self.snooze_picker.current(0)
        self.snooze_picker.pack(anchor='w',pady=(10,6))
        ttk.Button(self.history_tab,text='Snooze selected alert',command=self.snooze_selected).pack(anchor='w')
        ttk.Label(self.history_tab,text='The month uses the display time zone above. Event editing uses the event’s own time zone.',wraplength=300).pack(anchor='w',pady=10)
        self.launch_at_sign_in=tk.BooleanVar(value=startup is not None and startup.enabled())
        toggle=ttk.Checkbutton(self.inspector,text='Reminder alerts at Windows sign-in',variable=self.launch_at_sign_in,command=self.change_startup)
        toggle.pack(anchor='w',pady=(10,5))
        if startup is None:
            toggle.state(['disabled'])
        service_controls=ttk.Frame(self.inspector)
        service_controls.pack(fill='x')
        if on_start:
            ttk.Button(service_controls,text='Start alerts',command=on_start).pack(side='left')
        if on_pause:
            ttk.Button(service_controls,text='Pause alerts',command=self.pause_alerts).pack(side='left',padx=6)
        ttk.Label(self.inspector,textvariable=self.alert_status,wraplength=300).pack(anchor='w',pady=6)
        self.status_label=ttk.Label(self,textvariable=self.status,wraplength=700)
        self.status_label.pack(fill='x',pady=(8,0))
        self.viewport.bind('<Configure>',self.layout)
        self.content.bind('<Configure>',self.content_changed)
        for widget in (self.viewport,self.grid_canvas,self.content):
            widget.bind('<MouseWheel>',lambda event:self.viewport.yview_scroll(-int(event.delta/120),'units'))
        self.bind('<Configure>',lambda event:self.status_label.configure(wraplength=max(250,self.winfo_width()-30)) if event.widget is self else None)
        self.new_event(reveal=False)
        self.refresh(force=True)

    def table(self,parent,columns,titles,height):
        frame=ttk.Frame(parent)
        frame.pack(fill='both',expand=True)
        tree=ttk.Treeview(frame,columns=columns,show='headings',selectmode='browse',height=height,style='RockyCalendar.Treeview')
        for column,title in zip(columns,titles):
            tree.heading(column,text=title)
            tree.column(column,width=80 if column=='time' else 220,stretch=column!='time',minwidth=55)
        scroll=ttk.Scrollbar(frame,orient='vertical',command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y')
        tree.pack(fill='both',expand=True)
        return tree

    def layout(self,event=None):
        width=self.viewport.winfo_width()
        narrow=width<1050
        self.viewport.itemconfigure(self.content_window,width=width)
        if narrow!=self.narrow or not getattr(self,'laid_out',False):
            self.narrow=narrow
            self.laid_out=True
            self.inspector.grid_forget()
            self.inspector.grid(row=1 if narrow else 0,column=0 if narrow else 1,sticky='nsew',padx=(0 if narrow else 12,0),pady=(12 if narrow else 0,0))
            self.content.columnconfigure(0,weight=3)
            self.content.columnconfigure(1,weight=0 if narrow else 1,minsize=0 if narrow else 330)
            self.content.rowconfigure(0,weight=0 if narrow else 1)
        self.content_changed()

    def content_changed(self,event=None):
        height=self.content.winfo_reqheight()
        if not self.narrow:
            height=max(height,self.viewport.winfo_height())
        current=int(float(self.viewport.itemcget(self.content_window,'height') or 0))
        if current!=height:
            self.viewport.itemconfigure(self.content_window,height=height)
        self.viewport.configure(scrollregion=self.viewport.bbox('all'))

    def form_values(self):
        return (self.body.get(),self.date.get(),self.clock.get(),self.event_zone.get(),self.repeat_picker.current())

    def can_leave(self):
        return (self.initial_form is None or self.form_values()==self.initial_form or
                messagebox.askyesno('Unsaved event','Discard the unsaved event changes?',parent=self.root))

    def new_event(self,reveal=True,check=True):
        if check and not self.can_leave():
            return False
        self.editor_row=None
        self.current_key=None
        self.day_list.selection_remove(*self.day_list.selection())
        self.body.set('')
        self.date.set(self.selected_day.isoformat())
        today=utc_now().astimezone(ZoneInfo(self.zone))
        self.clock.set(f'{min(23,today.hour+1):02d}:00' if self.selected_day==today.date() and today.hour<23 else
                       '23:59' if self.selected_day==today.date() else '09:00')
        self.event_zone.set(self.zone)
        self.repeat_picker.current(0)
        self.mode.set('New event')
        for field in self.fields:
            field.state(['!disabled'])
        self.repeat_picker.state(['!disabled'])
        self.save_button.state(['!disabled'])
        self.cancel_button.state(['disabled'])
        self.done_button.state(['disabled'])
        self.initial_form=self.form_values()
        self.tabs.select(self.edit_tab)
        self.day_title.set(self.selected_day.strftime('%A, %d %B'))
        if reveal:
            self.reveal_editor()
        return True

    def reveal_editor(self):
        if self.narrow:
            self.update_idletasks()
            total=max(1,self.content.winfo_height())
            self.viewport.yview_moveto(self.inspector.winfo_y()/total)

    def select_day(self,day,key=None):
        if day!=self.selected_day or key!=self.current_key:
            if not self.can_leave():
                return False
        self.selected_day=day
        self.fill_day(preserve=False)
        if key:
            self.select_event(key,check=False)
        else:
            self.new_event(check=False)
        self.draw_month()
        return True

    def select_event(self,key,check=True):
        if key not in self.events_by_key:
            return
        if check and not self.can_leave():
            return
        event=self.events_by_key[key]
        row=self.store.reminder(event.reminder_id)
        if row is None:
            self.status.set('That event is unavailable. Refresh the calendar.')
            return
        row=dict(row)
        self.editor_row=row
        self.current_key=key
        self.day_list.selection_set(key)
        pending=row['notified_at'] is None
        self.body.set(row['body'] if pending else event.body)
        due=datetime.fromtimestamp(row['due_at'],ZoneInfo(row['timezone'])) if pending else event.due.astimezone(ZoneInfo(self.zone))
        self.date.set(due.strftime('%Y-%m-%d'))
        self.clock.set(due.strftime('%H:%M'))
        self.event_zone.set(row['timezone'] if pending else self.zone)
        self.repeat_picker.current(list(REPEATS).index(row['repeat'] if pending else event.repeat))
        self.mode.set('Edit series — all future occurrences' if pending and row['repeat']!='once' else
                      'Edit event' if pending else 'Past event — already delivered or completed')
        for field in self.fields:
            field.state(['!disabled'] if pending else ['disabled'])
        self.repeat_picker.state(['!disabled'] if pending else ['disabled'])
        self.save_button.state(['!disabled'] if pending else ['disabled'])
        self.cancel_button.state(['!disabled'] if pending else ['disabled'])
        self.done_button.state(['!disabled'] if pending and row['repeat']=='until_done' else ['disabled'])
        self.initial_form=self.form_values()
        if pending and row['repeat']!='once':
            self.status.set('This is a series. The editor shows its next stored alert; changes apply to all future occurrences.')
        else:
            self.status.set('Edit the event below.' if pending else 'Past alerts are read-only. Use New event to schedule another reminder.')
        self.tabs.select(self.edit_tab)
        self.reveal_editor()

    def list_select(self,event=None):
        selected=self.day_list.selection()
        if not selected or selected[0]==self.current_key:
            return
        previous=self.current_key
        if self.can_leave():
            self.select_event(selected[0],check=False)
        elif previous and self.day_list.exists(previous):
            self.day_list.selection_set(previous)
        else:
            self.day_list.selection_remove(*selected)

    def fill_day(self,preserve=True):
        self.day_title.set(self.selected_day.strftime('%A, %d %B'))
        selected=self.day_list.selection() if preserve else ()
        self.day_list.delete(*self.day_list.get_children())
        for event in self.grouped.get(self.selected_day,[]):
            label=event.body+(' · past alert' if event.delivered else ' · repeats' if event.preview else '')
            self.day_list.insert('', 'end',iid=event.key,values=(event.due.astimezone(ZoneInfo(self.zone)).strftime('%H:%M'),label))
        if selected and self.day_list.exists(selected[0]):
            self.day_list.selection_set(selected[0])

    def refresh(self,force=False):
        if self.service_status:
            self.alert_status.set('Background reminder alerts are active.' if self.service_status() else 'Alerts are paused or starting. Use Start alerts if needed.')
        self.days=month_days(self.year,self.month)
        start,end=view_bounds(self.days,self.zone)
        try:
            pending,alerts,recent=load_calendar(self.store,start,end,self.zone)
            signature=(self.year,self.month,self.zone,pending,alerts,recent,utc_now().astimezone(ZoneInfo(self.zone)).date())
            if not force and signature==self.signature:
                return
            self.signature=signature
            limit=len(pending)>2000 or len(alerts)>2000
            events=schedule_events(pending[:2000],alerts[:2000],start,end)
            self.grouped=group_days(events,self.zone)
            self.events_by_key={event.key:event for values in self.grouped.values() for event in values}
            self.month_title.set(f'{calendar.month_name[self.month]} {self.year}')
            self.fill_day()
            self.history_rows={str(row['id']):row for row in recent}
            selected=self.history_list.selection()
            self.history_list.delete(*self.history_list.get_children())
            for key,row in self.history_rows.items():
                self.history_list.insert('','end',iid=key,values=(display_time(row['notified_at'],self.zone),row['body']))
            if selected and self.history_list.exists(selected[0]):
                self.history_list.selection_set(selected[0])
            self.draw_month()
            if limit:
                self.status.set('Calendar preview limited to 2,000 pending events and 2,000 past alerts.')
        except (OSError,sqlite3.Error,ValueError,KeyError) as exc:
            self.status.set(f'Could not load calendar: {exc}')

    def draw_month(self):
        if not hasattr(self,'days'):
            return
        canvas=self.grid_canvas
        canvas.delete('all')
        width=max(1,canvas.winfo_width())
        height=max(1,canvas.winfo_height())
        heading=self.week_font.metrics('linespace')+14
        cell_width=width/7
        cell_height=max(1,(height-heading)/6)
        zone=ZoneInfo(self.zone)
        today=utc_now().astimezone(zone).date()
        for column,name in enumerate(calendar.day_name):
            label=name if self.week_font.measure(name)+12<cell_width else name[:3]
            canvas.create_text((column+.5)*cell_width,heading/2,text=label,fill=MUTED,font=self.week_font)
        self.hit_days={}
        for index,day in enumerate(self.days):
            if day is None:
                continue
            col,row=index%7,index//7
            x,y=col*cell_width,heading+row*cell_height
            tag='day:'+day.isoformat()
            self.hit_days[tag]=day
            fill='#29223e' if day==self.selected_day else CELL if day.month==self.month else '#161624'
            canvas.create_rectangle(x+2,y+2,x+cell_width-2,y+cell_height-2,fill=fill,
                                    outline=VIOLET if day==self.selected_day else '#343248',width=2 if day==self.selected_day else 1,tags=(tag,))
            canvas.create_text(x+10,y+7,anchor='nw',text=str(day.day),font=self.day_font,
                               fill=MINT if day==today else TEXT if day.month==self.month else '#77748e',tags=(tag,))
            values=self.grouped.get(day,[])
            line=self.event_font.metrics('linespace')+6
            top=y+self.day_font.metrics('linespace')+12
            maximum=max(0,min(3,int((y+cell_height-top-8)/line)))
            if len(values)>maximum and maximum>0:
                maximum-=1
            for position,event in enumerate(values[:maximum]):
                py=top+position*line
                event_tag='event:'+event.key
                color='#343044' if event.delivered else '#302747' if event.preview else '#23483e'
                canvas.create_rectangle(x+6,py,x+cell_width-6,py+line-3,fill=color,outline='',tags=(tag,event_tag))
                label=event.due.astimezone(zone).strftime('%H:%M')+' '+event.body.replace('\n',' ')[:100]
                while label and self.event_font.measure(label+'…')>max(10,cell_width-22):
                    label=label[:-1]
                shown=label+'…' if label!=event.due.astimezone(zone).strftime('%H:%M')+' '+event.body.replace('\n',' ')[:100] else label
                canvas.create_text(x+10,py+(line-3)/2,anchor='w',text=shown,font=self.event_font,
                                   fill=MUTED if event.delivered else TEXT,tags=(tag,event_tag))
            if len(values)>maximum:
                canvas.create_text(x+8,y+cell_height-7,anchor='sw',text=f'+{len(values)-maximum} more' if maximum else f'● {len(values)} event'+('s' if len(values)!=1 else ''),
                                   font=self.event_font,fill=MINT,tags=(tag,))

    def grid_click(self,event):
        items=self.grid_canvas.find_withtag('current')
        if not items:
            return
        tags=self.grid_canvas.gettags(items[-1])
        day=next((self.hit_days[tag] for tag in tags if tag in self.hit_days),None)
        key=next((tag[len('event:'):] for tag in tags if tag.startswith('event:')),None)
        if day:
            self.select_day(day,key)

    def change_month(self,offset):
        if not self.can_leave():
            return
        try:
            self.year,self.month=month_shift(self.year,self.month,offset)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.selected_day=self.selected_day.replace(year=self.year,month=self.month,day=1)
        self.new_event(reveal=False,check=False)
        self.refresh(force=True)
        self.viewport.yview_moveto(0)

    def go_today(self):
        if not self.can_leave():
            return
        self.selected_day=utc_now().astimezone(ZoneInfo(self.zone)).date()
        self.year,self.month=self.selected_day.year,self.selected_day.month
        self.new_event(reveal=False,check=False)
        self.refresh(force=True)
        self.viewport.yview_moveto(0)

    def change_zone(self,event=None):
        entered=self.view_zone.get().strip()
        try:
            ZoneInfo(entered)
        except (ValueError,KeyError):
            self.view_zone.set(self.zone)
            self.status.set('Enter an IANA time zone, such as Asia/Kolkata or UTC.')
            return
        if not self.can_leave():
            self.view_zone.set(self.zone)
            return
        self.zone=entered
        self.new_event(reveal=False,check=False)
        self.refresh(force=True)

    def save_event(self):
        row=self.editor_row
        body=self.body.get().strip()
        when=self.date.get().strip()+' '+self.clock.get().strip()
        zone=self.event_zone.get().strip()
        try:
            if not body or len(body)>1000:
                raise ValueError('Enter an event name of 1–1,000 characters.')
            repeat=list(REPEATS)[self.repeat_picker.current()]
            original=datetime.fromtimestamp(row['due_at'],timezone.utc) if row else None
            if row and when==display_time(row['due_at'],row['timezone']) and zone==row['timezone'] and original>utc_now():
                due=original
            else:
                due=event_time(when,zone)
            if row:
                if not self.store.update_reminder(row['id'],body,due,zone,created_at=row['created_at'],repeat=repeat,expected=row):
                    raise ValueError('That event changed or was delivered. Select it again before saving.')
                event_id=row['id']
            else:
                event_id=self.store.add_reminder(body,due,zone,body,repeat=repeat)
            updated=dict(self.store.reminder(event_id))
        except (OSError,ValueError,KeyError,sqlite3.Error) as exc:
            self.status.set(f'Could not save event: {exc}')
            return False
        if row is None:
            self.selected_day=datetime.fromtimestamp(updated['due_at'],ZoneInfo(self.zone)).date()
            self.year,self.month=self.selected_day.year,self.selected_day.month
        self.editor_row=updated
        self.initial_form=self.form_values()
        self.refresh(force=True)
        current=next((event.key for event in self.grouped.get(self.selected_day,[]) if event.reminder_id==event_id and not event.delivered),None)
        if current:
            self.select_event(current,check=False)
        self.status.set('Event saved. Reminder alerts use this schedule.')
        self.on_notify('Calendar event saved.')
        return True

    def cancel_event(self):
        row=self.editor_row
        if row is None or row['notified_at'] is not None:
            return
        prompt=f'Cancel “{row["body"]}”'+(' and all future repeats?' if row['repeat']!='once' else '?')
        if not messagebox.askyesno('Cancel event',prompt,parent=self.root):
            return
        try:
            if not self.store.cancel_reminder(row['id'],created_at=row['created_at'],expected=row):
                raise ValueError('That event changed. Select it again.')
            self.new_event(reveal=False,check=False)
            self.refresh(force=True)
            self.status.set('Event cancelled.')
            self.on_notify('Calendar event cancelled.')
        except (ValueError,sqlite3.Error) as exc:
            self.status.set(str(exc))

    def complete_event(self):
        row=self.editor_row
        if row is None:
            return
        try:
            if not self.store.complete_reminder(row['id'],expected=row):
                raise ValueError('That event changed or is not a pending until-done reminder. Select it again.')
            self.new_event(reveal=False,check=False)
            self.refresh(force=True)
            self.status.set('Marked done. Repeat alerts stopped.')
            self.on_notify('Reminder marked done.')
        except (ValueError,sqlite3.Error) as exc:
            self.status.set(str(exc))

    def snooze_selected(self):
        selected=self.history_list.selection()
        if not selected:
            self.status.set('Select a recent alert first.')
            return
        try:
            minutes=(5,15,30)[self.snooze_picker.current()]
            self.store.snooze_alert(int(selected[0]),minutes)
            self.refresh(force=True)
            self.status.set(f'Alert snoozed for {minutes} minutes.')
            self.on_notify(f'Reminder snoozed for {minutes} minutes.')
        except (ValueError,sqlite3.Error) as exc:
            self.status.set(str(exc))

    def change_startup(self):
        try:
            self.startup.set_enabled(self.launch_at_sign_in.get())
            self.status.set('Reminder sign-in setting saved.')
        except Exception as exc:
            self.launch_at_sign_in.set(self.startup.enabled())
            self.status.set(f'Could not change sign-in setting: {exc}')

    def pause_alerts(self):
        if messagebox.askyesno('Pause reminder alerts','Pause background reminder alerts? Events stay saved. Use Start alerts to resume.',parent=self.root):
            self.on_pause()
