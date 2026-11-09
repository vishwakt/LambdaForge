FROM public.ecr.aws/lambda/python:3.14

# Copy requirements and install dependencies (cached as Docker layer)
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r requirements.txt

# Install every strategy pack in packs/ (see packs/README.md); with no packs
# this installs nothing. A pack may add libraries but can't change the
# engine's: the freeze pins every version already installed.
COPY packs/ /tmp/packs/
RUN if ls -d /tmp/packs/*/ > /dev/null 2>&1; then \
        pip freeze > /tmp/engine-versions.txt && \
        pip install --no-cache-dir -c /tmp/engine-versions.txt /tmp/packs/*/; \
    fi && rm -rf /tmp/packs /tmp/engine-versions.txt

# Copy application code
COPY handler.py ${LAMBDA_TASK_ROOT}/
COPY src/ ${LAMBDA_TASK_ROOT}/src/
COPY config.json bots.json ${LAMBDA_TASK_ROOT}/

# A broken pack or a reused strategy name fails the build, not the bot.
RUN python -B -c "from src.strategies import STRATEGIES"

# Default handler (overridden per function in template.yaml)
CMD ["handler.daily_scan_handler"]
