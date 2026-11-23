"""Nonblocking local model preloading; never changes the game or user data."""
import logging
import os
import threading
import time
from model_profile import selected_model

def schedule():
    def run():
        from ollama_provider import _local_model_post
        started=time.monotonic()
        try:
            response=_local_model_post('http://127.0.0.1:11434/api/chat',json={
                'model':selected_model(),'messages':[],'stream':False,'keep_alive':'10m',
                'options':{'num_predict':0,'num_ctx':2048,
                           'num_thread':int(os.getenv('EYES_OLLAMA_THREADS','2'))}},timeout=60)
            response.raise_for_status()
            data=response.json()
            logging.info('ollama_warmup status=ready seconds=%.3f load_seconds=%.3f',
                         time.monotonic()-started,data.get('load_duration',0)/1e9)
        except Exception as error:
            logging.info('ollama_warmup status=unavailable cause=%s seconds=%.3f',
                         type(error).__name__,time.monotonic()-started)
    worker=threading.Thread(target=run,name='eyes-model-warmup',daemon=True)
    worker.start()
    return worker
