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
