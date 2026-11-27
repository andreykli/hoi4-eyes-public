"""Presentation-only native chat helpers; no engine or storage dependencies."""
from datetime import datetime
import tkinter as tk

def message_time(item):
    value=item.get('timestamp') or item.get('created_at')
    if not isinstance(value,str):return ''
    try:
        stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
        if stamp.tzinfo is not None:stamp=stamp.astimezone()
        return stamp.strftime('%d.%m.%Y %H:%M')
    except (ValueError,OverflowError):return ''

def scroll_state(widget,same_chat):
    try:
        first,last=widget.yview()
        return not same_chat or float(last)>=.97,float(first)
    except (AttributeError,TypeError,ValueError):return True,0.0

class ChatComposer(tk.Text):
    """Multiline Text with the small Entry interface used by existing send code."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,wrap='word',height=3,undo=True,**kwargs)
    @staticmethod
    def position(index):
        return '1.0 + '+str(index)+' chars' if isinstance(index,int) else 'end-1c' if index=='end' else index
    def get(self,*args):return super().get(*(args or ('1.0','end-1c')))
    def insert(self,index,text,*args):return super().insert(self.position(index),text,*args)
    def delete(self,first,last=None):
        return super().delete(self.position(first),self.position(last) if last is not None else None)
    def selection_present(self):return bool(self.tag_ranges('sel'))
    def selection_range(self,first,last):self.tag_add('sel',self.position(first),self.position(last))
    def icursor(self,index):self.mark_set('insert',self.position(index))

class GroupParticipants(tk.Frame):
    """Participant selection embedded in the existing HOI4 diplomacy window."""
    def __init__(self, parent, apply_callback, changed_callback, saved_callback=None):
        from tkinter import ttk
        from ui_locale import tr
        super().__init__(parent,bg='#172118',padx=8,pady=5)
        self.apply_callback=apply_callback;self.changed_callback=changed_callback
        self.members=[];self.original=();self.chat_id=None;self.available={};self.enabled=False
        self.caption=tk.Label(self,text='',bg='#172118',fg='#d8c38e',anchor='w',font=('Segoe UI',9))
        self.caption.grid(row=0,column=0,columnspan=4,sticky='ew')
        self.choice=ttk.Combobox(self,state='readonly',width=28)
        self.choice.grid(row=1,column=0,columnspan=2,sticky='ew',pady=4,padx=(0,6))
        self.add_button=tk.Button(self,text=tr('Add country'),command=self.add_country,bg='#35412e',fg='#ead9af',relief='flat')
        self.add_button.grid(row=1,column=2,padx=3)
        self.apply_button=tk.Button(self,text=tr('Apply participants'),command=self.submit,bg='#344c4c',fg='#f2f0e8',relief='flat')
        self.apply_button.grid(row=1,column=3,padx=3)
        self.chips=tk.Frame(self,bg='#172118');self.chips.grid(row=2,column=0,columnspan=4,sticky='ew')
        self.separate=tk.BooleanVar(value=True)
        self.separate_button=tk.Checkbutton(self,text=tr('Start a separate negotiation'),variable=self.separate,
            command=self.refresh,bg='#172118',fg='#cbd2c1',selectcolor='#35412e',activebackground='#172118',anchor='w')
        self.separate_button.grid(row=3,column=0,columnspan=3,sticky='w')
        self.saved_button=tk.Button(self,text=tr('Saved negotiations'),command=saved_callback,
            bg='#35412e',fg='#ead9af',relief='flat')
        self.saved_button.grid(row=3,column=3,padx=3)
        self.columnconfigure(0,weight=1);self.columnconfigure(1,weight=1)

    @property
    def dirty(self):
        return tuple(sorted(self.members)) != self.original

    def set_chat(self, chat, enabled):
        from group_negotiation import country_options
        if not self.available:self.available=country_options()
        members=[tag for tag in chat.get('participants',[]) if tag != chat.get('player_country')]
        signature=(chat.get('chat_id'),tuple(sorted(members)))
        if signature != (self.chat_id,self.original):
            self.chat_id,self.original=signature
            self.members=members
            self.separate.set(not chat.get('group_negotiation') or bool(chat.get('agreements') or chat.get('territory_proposals')))
        self.player=chat.get('player_country');self.enabled=bool(enabled)
        self.refresh()

    def set_enabled(self, enabled):
        self.enabled=bool(enabled);self.refresh()

    def refresh(self):
        from ui_locale import tr
        from group_negotiation import MAX_AI_COUNTRIES
        signature=(tuple(self.members),getattr(self,'player',None),self.enabled,bool(self.separate.get()),tuple(sorted(self.available.items())))
        if getattr(self,'_rendered_signature',None)==signature:return
        self._rendered_signature=signature
        values=sorted(name for tag,name in self.available.items() if tag not in self.members and tag != getattr(self,'player',None))
        self.choice.configure(values=values,state='readonly' if self.enabled and len(self.members)<MAX_AI_COUNTRIES else 'disabled')
        if self.choice.get() not in values:self.choice.set(values[0] if values else '')
        self.caption.configure(text=f"{tr('Group negotiation')} · {tr('Countries selected')}: {len(self.members)} / {MAX_AI_COUNTRIES}")
        self.add_button.configure(state='normal' if self.enabled and len(self.members)<MAX_AI_COUNTRIES else 'disabled')
        self.apply_button.configure(state='normal' if self.enabled and 1<=len(self.members)<=MAX_AI_COUNTRIES and (self.dirty or self.separate.get()) else 'disabled')
        self.separate_button.configure(state='normal' if self.enabled else 'disabled')
        self.saved_button.configure(state='normal' if self.enabled else 'disabled')
        for child in self.chips.winfo_children():child.destroy()
        for index,tag in enumerate(self.members):
            button=tk.Button(self.chips,text=self.available.get(tag,tag)+' ×',command=lambda value=tag:self.remove_country(value),
                bg='#303b2b',fg='#f2f0e8',relief='flat',anchor='w',wraplength=180,
                state='normal' if self.enabled else 'disabled')
            button.grid(row=index//3,column=index%3,sticky='ew',padx=2,pady=2)
        for column in range(3):self.chips.columnconfigure(column,weight=1,uniform='country')

    def add_country(self):
        if not self.enabled or len(self.members)>=6:return
        tag=next((tag for tag,name in self.available.items() if name==self.choice.get() and tag not in self.members and tag!=self.player),None)
        if tag:self.members.append(tag);self.refresh();self.changed_callback()

    def remove_country(self, tag):
        if not self.enabled or tag not in self.members:return
        self.members.remove(tag);self.refresh();self.changed_callback()

    def submit(self):
        if self.enabled and 1<=len(self.members)<=6:
            self.apply_callback(list(self.members),bool(self.separate.get()))
