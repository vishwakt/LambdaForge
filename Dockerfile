FROM public.ecr.aws/lambda/python:3.9

# Copy requirements and install dependencies (cached as Docker layer)
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY handler.py ${LAMBDA_TASK_ROOT}/
COPY src/ ${LAMBDA_TASK_ROOT}/src/
COPY config.json ${LAMBDA_TASK_ROOT}/

# Default handler (overridden per function in template.yaml)
CMD ["handler.daily_scan_handler"]
