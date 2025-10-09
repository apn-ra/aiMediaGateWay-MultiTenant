# Author: RA
# Purpose: Mixins
# Created: 09/10/2025

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt


class CsrfExemptMixin:
    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)
