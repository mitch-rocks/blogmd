# Use an official lightweight Python image
FROM python:3.12-slim

# Set the working directory inside the container
WORKDIR /data

# Copy only the requirements file first to leverage Docker cache
COPY ./requirements.txt /data/requirements.txt

# Install dependencies
RUN pip install --no-cache-dir --upgrade -r /data/requirements.txt

# Copy the application code
COPY . .

# Expose the port Uvicorn will run on
EXPOSE 8000

# Command to run the Uvicorn server
CMD ["python", "src/main.py"]
