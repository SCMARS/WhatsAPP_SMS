FROM node:20-alpine

WORKDIR /app

# Install dependencies only if package.json changes
COPY package.json package-lock.json ./
RUN npm install

# Copy everything else
COPY . .

# Vite default dev port is 5173. 
# We need to expose it and run with --host so it's accessible externally.
EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host"]
