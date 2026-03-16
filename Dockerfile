FROM public.ecr.aws/lambda/python:3.9

# Copy requirements and install dependencies
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ${LAMBDA_TASK_ROOT}/src/
COPY config.json ${LAMBDA_TASK_ROOT}/

# Default handler (overridden per function in template.yaml)
CMD ["src.lambda_handlers.daily_scan_handler"]
